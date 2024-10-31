import functools
import sys
from importlib import import_module
from importlib.metadata import version
from packaging.version import Version, parse
from .log_config import logger
from .event import LLMEvent, ActionEvent, ToolEvent, ErrorEvent
from .helpers import get_ISO_time, check_call_stack_for_agent_id
import inspect
from typing import Optional
import pprint

original_create = None
original_create_async = None


class LlmTracker:
    SUPPORTED_APIS = {
        "litellm": {"1.3.1": ("openai_chat_completions.completion",)},
        "openai": {
            "1.0.0": ("chat.completions.create",),
            "0.0.0": (
                "ChatCompletion.create",
                "ChatCompletion.acreate",
            ),
        },
        "cohere": {
            "5.4.0": ("chat", "chat_stream"),
        },
    }

    def __init__(self, client):
        self.client = client
        self.completion = ""
        self.llm_event: Optional[LLMEvent] = None
        self.active_providers = {}  # Track active providers
        self.original_methods = {}  # Store original methods per provider

    def _handle_response_v0_openai(self, response, kwargs, init_timestamp):
        """Handle responses for OpenAI versions <v1.0.0"""

        self.llm_event = LLMEvent(init_timestamp=init_timestamp, params=kwargs)

        def handle_stream_chunk(chunk):
            # NOTE: prompt/completion usage not returned in response when streaming
            # We take the first ChatCompletionChunk and accumulate the deltas from all subsequent chunks to build one full chat completion
            if self.llm_event.returns == None:
                self.llm_event.returns = chunk

            try:
                accumulated_delta = self.llm_event.returns["choices"][0]["delta"]
                self.llm_event.agent_id = check_call_stack_for_agent_id()
                self.llm_event.model = chunk["model"]
                self.llm_event.prompt = kwargs["messages"]
                choice = chunk["choices"][
                    0
                ]  # NOTE: We assume for completion only choices[0] is relevant

                if choice["delta"].get("content"):
                    accumulated_delta["content"] += choice["delta"].content

                if choice["delta"].get("role"):
                    accumulated_delta["role"] = choice["delta"].get("role")

                if choice["finish_reason"]:
                    # Streaming is done. Record LLMEvent
                    self.llm_event.returns.choices[0]["finish_reason"] = choice[
                        "finish_reason"
                    ]
                    self.llm_event.completion = {
                        "role": accumulated_delta["role"],
                        "content": accumulated_delta["content"],
                    }
                    self.llm_event.end_timestamp = get_ISO_time()

                    self.client.record(self.llm_event)
            except Exception as e:
                self.client.record(
                    ErrorEvent(trigger_event=self.llm_event, exception=e)
                )
                kwargs_str = pprint.pformat(kwargs)
                chunk = pprint.pformat(chunk)
                logger.warning(
                    f"Unable to parse a chunk for LLM call. Skipping upload to AgentOps\n"
                    f"chunk:\n {chunk}\n"
                    f"kwargs:\n {kwargs_str}\n"
                )

        # if the response is a generator, decorate the generator
        if inspect.isasyncgen(response):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)

                    yield chunk

            return async_generator()

        elif inspect.isgenerator(response):

            def generator():
                for chunk in response:
                    handle_stream_chunk(chunk)

                    yield chunk

            return generator()

        # v0.0.0 responses are dicts
        try:
            self.llm_event.returns = response
            self.llm_event.agent_id = check_call_stack_for_agent_id()
            self.llm_event.prompt = kwargs["messages"]
            self.llm_event.prompt_tokens = response["usage"]["prompt_tokens"]
            self.llm_event.completion = {
                "role": "assistant",
                "content": response["choices"][0]["message"]["content"],
            }
            self.llm_event.completion_tokens = response["usage"]["completion_tokens"]
            self.llm_event.model = response["model"]
            self.llm_event.end_timestamp = get_ISO_time()

            self.client.record(self.llm_event)
        except Exception as e:
            self.client.record(ErrorEvent(trigger_event=self.llm_event, exception=e))
            kwargs_str = pprint.pformat(kwargs)
            response = pprint.pformat(response)
            logger.warning(
                f"Unable to parse response for LLM call. Skipping upload to AgentOps\n"
                f"response:\n {response}\n"
                f"kwargs:\n {kwargs_str}\n"
            )

        return response

    def _handle_response_v1_openai(self, response, kwargs, init_timestamp):
        """Handle responses for OpenAI versions >v1.0.0"""
        from openai import Stream, AsyncStream
        from openai.types.chat import ChatCompletionChunk
        from openai.resources import AsyncCompletions

        self.llm_event = LLMEvent(init_timestamp=init_timestamp, params=kwargs)

        def handle_stream_chunk(chunk: ChatCompletionChunk):
            # NOTE: prompt/completion usage not returned in response when streaming
            # We take the first ChatCompletionChunk and accumulate the deltas from all subsequent chunks to build one full chat completion
            if self.llm_event.returns == None:
                self.llm_event.returns = chunk

            try:
                accumulated_delta = self.llm_event.returns.choices[0].delta
                self.llm_event.agent_id = check_call_stack_for_agent_id()
                self.llm_event.model = chunk.model
                self.llm_event.prompt = kwargs["messages"]
                choice = chunk.choices[
                    0
                ]  # NOTE: We assume for completion only choices[0] is relevant

                if choice.delta.content:
                    accumulated_delta.content += choice.delta.content

                if choice.delta.role:
                    accumulated_delta.role = choice.delta.role

                if choice.delta.tool_calls:
                    accumulated_delta.tool_calls = choice.delta.tool_calls

                if choice.delta.function_call:
                    accumulated_delta.function_call = choice.delta.function_call

                if choice.finish_reason:
                    # Streaming is done. Record LLMEvent
                    self.llm_event.returns.choices[0].finish_reason = (
                        choice.finish_reason
                    )
                    self.llm_event.completion = {
                        "role": accumulated_delta.role,
                        "content": accumulated_delta.content,
                        "function_call": accumulated_delta.function_call,
                        "tool_calls": accumulated_delta.tool_calls,
                    }
                    self.llm_event.end_timestamp = get_ISO_time()

                    self.client.record(self.llm_event)
            except Exception as e:
                self.client.record(
                    ErrorEvent(trigger_event=self.llm_event, exception=e)
                )
                kwargs_str = pprint.pformat(kwargs)
                chunk = pprint.pformat(chunk)
                logger.warning(
                    f"Unable to parse a chunk for LLM call. Skipping upload to AgentOps\n"
                    f"chunk:\n {chunk}\n"
                    f"kwargs:\n {kwargs_str}\n"
                )

        # if the response is a generator, decorate the generator
        if isinstance(response, Stream):

            def generator():
                for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return generator()

        # For asynchronous AsyncStream
        elif isinstance(response, AsyncStream):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # For async AsyncCompletion
        elif isinstance(response, AsyncCompletions):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # v1.0.0+ responses are objects
        try:
            self.llm_event.returns = response.model_dump()
            self.llm_event.agent_id = check_call_stack_for_agent_id()
            self.llm_event.prompt = kwargs["messages"]
            self.llm_event.prompt_tokens = response.usage.prompt_tokens
            self.llm_event.completion = response.choices[0].message.model_dump()
            self.llm_event.completion_tokens = response.usage.completion_tokens
            self.llm_event.model = response.model

            self.client.record(self.llm_event)
        except Exception as e:
            self.client.record(ErrorEvent(trigger_event=self.llm_event, exception=e))
            kwargs_str = pprint.pformat(kwargs)
            response = pprint.pformat(response)
            logger.warning(
                f"Unable to parse response for LLM call. Skipping upload to AgentOps\n"
                f"response:\n {response}\n"
                f"kwargs:\n {kwargs_str}\n"
            )

        return response

    def _handle_response_cohere(self, response, kwargs, init_timestamp):
        """Handle responses for Cohere versions >v5.4.0"""
        from cohere.types.non_streamed_chat_response import NonStreamedChatResponse
        from cohere.types.streamed_chat_response import (
            StreamedChatResponse,
            StreamedChatResponse_CitationGeneration,
            StreamedChatResponse_SearchQueriesGeneration,
            StreamedChatResponse_SearchResults,
            StreamedChatResponse_StreamEnd,
            StreamedChatResponse_StreamStart,
            StreamedChatResponse_TextGeneration,
            StreamedChatResponse_ToolCallsGeneration,
        )

        # from cohere.types.chat import ChatGenerationChunk

        # NOTE: Cohere only returns one message and its role will be CHATBOT which we are coercing to "assistant"
        self.llm_event = LLMEvent(init_timestamp=init_timestamp, params=kwargs)

        self.action_events = {}

        def handle_stream_chunk(chunk):

            # We take the first chunk and accumulate the deltas from all subsequent chunks to build one full chat completion
            if isinstance(chunk, StreamedChatResponse_StreamStart):
                self.llm_event.returns = chunk
                self.llm_event.agent_id = check_call_stack_for_agent_id()
                self.llm_event.model = kwargs.get("model", "command-r-plus")
                self.llm_event.prompt = kwargs["message"]
                self.llm_event.completion = ""
                return

            try:
                if isinstance(chunk, StreamedChatResponse_StreamEnd):
                    # StreamedChatResponse_TextGeneration = LLMEvent
                    self.llm_event.completion = {
                        "role": "assistant",
                        "content": chunk.response.text,
                    }
                    self.llm_event.end_timestamp = get_ISO_time()
                    self.client.record(self.llm_event)

                    # StreamedChatResponse_SearchResults = ActionEvent
                    search_results = chunk.response.search_results
                    for search_result in search_results:
                        query = search_result.search_query
                        if query.generation_id in self.action_events:
                            action_event = self.action_events[query.generation_id]
                            search_result_dict = search_result.dict()
                            del search_result_dict["search_query"]
                            action_event.returns = search_result_dict
                            action_event.end_timestamp = get_ISO_time()

                    # StreamedChatResponse_CitationGeneration = ActionEvent
                    documents = {doc["id"]: doc for doc in chunk.response.documents}
                    citations = chunk.response.citations
                    for citation in citations:
                        citation_id = f"{citation.start}.{citation.end}"
                        if citation_id in self.action_events:
                            action_event = self.action_events[citation_id]
                            citation_dict = citation.dict()
                            # Replace document_ids with the actual documents
                            citation_dict["documents"] = [
                                documents[doc_id]
                                for doc_id in citation_dict["document_ids"]
                                if doc_id in documents
                            ]
                            del citation_dict["document_ids"]

                            action_event.returns = citation_dict
                            action_event.end_timestamp = get_ISO_time()

                    for key, action_event in self.action_events.items():
                        self.client.record(action_event)

                elif isinstance(chunk, StreamedChatResponse_TextGeneration):
                    self.llm_event.completion += chunk.text
                elif isinstance(chunk, StreamedChatResponse_ToolCallsGeneration):
                    pass
                elif isinstance(chunk, StreamedChatResponse_CitationGeneration):
                    for citation in chunk.citations:
                        self.action_events[f"{citation.start}.{citation.end}"] = (
                            ActionEvent(
                                action_type="citation",
                                init_timestamp=get_ISO_time(),
                                params=citation.text,
                            )
                        )
                elif isinstance(chunk, StreamedChatResponse_SearchQueriesGeneration):
                    for query in chunk.search_queries:
                        self.action_events[query.generation_id] = ActionEvent(
                            action_type="search_query",
                            init_timestamp=get_ISO_time(),
                            params=query.text,
                        )
                elif isinstance(chunk, StreamedChatResponse_SearchResults):
                    pass

            except Exception as e:
                self.client.record(
                    ErrorEvent(trigger_event=self.llm_event, exception=e)
                )
                kwargs_str = pprint.pformat(kwargs)
                chunk = pprint.pformat(chunk)
                logger.warning(
                    f"Unable to parse a chunk for LLM call. Skipping upload to AgentOps\n"
                    f"chunk:\n {chunk}\n"
                    f"kwargs:\n {kwargs_str}\n"
                )

        # NOTE: As of Cohere==5.x.x, async is not supported
        # if the response is a generator, decorate the generator
        if inspect.isasyncgen(response):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        elif inspect.isgenerator(response):

            def generator():
                for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return generator()

        # TODO: we should record if they pass a chat.connectors, because it means they intended to call a tool
        # Not enough to record StreamedChatResponse_ToolCallsGeneration because the tool may have not gotten called

        try:
            self.llm_event.returns = response.dict()
            self.llm_event.agent_id = check_call_stack_for_agent_id()
            self.llm_event.prompt = []
            if response.chat_history:
                role_map = {"USER": "user", "CHATBOT": "assistant", "SYSTEM": "system"}

                for i in range(len(response.chat_history) - 1):
                    message = response.chat_history[i]
                    self.llm_event.prompt.append(
                        {
                            "role": role_map.get(message.role, message.role),
                            "content": message.message,
                        }
                    )

                last_message = response.chat_history[-1]
                self.llm_event.completion = {
                    "role": role_map.get(last_message.role, last_message.role),
                    "content": last_message.message,
                }
            self.llm_event.prompt_tokens = response.meta.tokens.input_tokens
            self.llm_event.completion_tokens = response.meta.tokens.output_tokens
            self.llm_event.model = kwargs.get("model", "command-r-plus")

            self.client.record(self.llm_event)
        except Exception as e:
            self.client.record(ErrorEvent(trigger_event=self.llm_event, exception=e))
            kwargs_str = pprint.pformat(kwargs)
            response = pprint.pformat(response)
            logger.warning(
                f"Unable to parse response for LLM call. Skipping upload to AgentOps\n"
                f"response:\n {response}\n"
                f"kwargs:\n {kwargs_str}\n"
            )

        return response

    def _store_original_method(self, provider: str, method_name: str, method):
        """Store original method with provider context"""
        if provider not in self.original_methods:
            self.original_methods[provider] = {}
        self.original_methods[provider][method_name] = method

    def _get_original_method(self, provider: str, method_name: str):
        """Retrieve original method for provider"""
        return self.original_methods.get(provider, {}).get(method_name)

    def override_openai_v1_completion(self):
        from openai.resources.chat import completions
        
        # Store with provider context
        self._store_original_method('openai', 'create', completions.Completions.create)
        self.active_providers['openai'] = True

        def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()
            # Get provider-specific original method
            original = self._get_original_method('openai', 'create')
            result = original(*args, **kwargs)
            return self._handle_response_v1_openai(result, kwargs, init_timestamp)

        completions.Completions.create = patched_function

    def override_litellm_completion(self):
        import litellm
        
        # Store with provider context
        self._store_original_method('litellm', 'completion', litellm.completion)
        self.active_providers['litellm'] = True

        def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()
            # Get provider-specific original method
            original = self._get_original_method('litellm', 'completion')
            result = original(*args, **kwargs)
            return self._handle_response_v1_openai(result, kwargs, init_timestamp)

        litellm.completion = patched_function

    def override_litellm_async_completion(self):
        import litellm
        
        # Store with provider context
        self._store_original_method('litellm', 'acompletion', litellm.acompletion)
        self.active_providers['litellm'] = True

        async def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()
            # Get provider-specific original method
            original = self._get_original_method('litellm', 'acompletion')
            result = await original(*args, **kwargs)
            return self._handle_response_v1_openai(result, kwargs, init_timestamp)

        litellm.acompletion = patched_function

    def override_cohere_chat(self):
        import cohere
        import cohere.types

        original_chat = cohere.Client.chat

        def patched_function(*args, **kwargs):
            # Call the original function with its original arguments
            init_timestamp = get_ISO_time()
            result = original_chat(*args, **kwargs)
            return self._handle_response_cohere(result, kwargs, init_timestamp)

        # Override the original method with the patched one
        cohere.Client.chat = patched_function

    def override_cohere_chat_stream(self):
        import cohere

        original_chat = cohere.Client.chat_stream

        def patched_function(*args, **kwargs):
            # Call the original function with its original arguments
            init_timestamp = get_ISO_time()
            result = original_chat(*args, **kwargs)
            return self._handle_response_cohere(result, kwargs, init_timestamp)

        # Override the original method with the patched one
        cohere.Client.chat_stream = patched_function

    def _override_method(self, api, method_path, module):
        def handle_response(result, kwargs, init_timestamp):
            if api == "openai":
                return self._handle_response_v0_openai(result, kwargs, init_timestamp)
            return result

        def wrap_method(original_method):
            if inspect.iscoroutinefunction(original_method):

                @functools.wraps(original_method)
                async def async_method(*args, **kwargs):
                    init_timestamp = get_ISO_time()
                    response = await original_method(*args, **kwargs)
                    return handle_response(response, kwargs, init_timestamp)

                return async_method

            else:

                @functools.wraps(original_method)
                def sync_method(*args, **kwargs):
                    init_timestamp = get_ISO_time()
                    response = original_method(*args, **kwargs)
                    return handle_response(response, kwargs, init_timestamp)

                return sync_method

        method_parts = method_path.split(".")
        original_method = functools.reduce(getattr, method_parts, module)
        new_method = wrap_method(original_method)

        if len(method_parts) == 1:
            setattr(module, method_parts[0], new_method)
        else:
            parent = functools.reduce(getattr, method_parts[:-1], module)
            setattr(parent, method_parts[-1], new_method)

    def override_api(self):
        """
        Overrides key methods of the specified API to record events.
        """

        for api in self.SUPPORTED_APIS:
            if api in sys.modules:
                module = import_module(api)
                if api == "litellm":
                    module_version = version(api)
                    if module_version is None:
                        logger.warning(
                            f"Cannot determine LiteLLM version. Only LiteLLM>=1.3.1 supported."
                        )

                    if Version(module_version) >= parse("1.3.1"):
                        self.override_litellm_completion()
                        self.override_litellm_async_completion()
                    else:
                        logger.warning(
                            f"Only LiteLLM>=1.3.1 supported. v{module_version} found."
                        )
                    continue  # Continue to check other APIs instead of returning

                if api == "openai":
                    # Patch openai v1.0.0+ methods
                    if hasattr(module, "__version__"):
                        module_version = parse(module.__version__)
                        if module_version >= parse("1.0.0"):
                            self.override_openai_v1_completion()
                            self.override_openai_v1_async_completion()
                        else:
                            # Patch openai <v1.0.0 methods
                            for method_path in self.SUPPORTED_APIS["openai"]["0.0.0"]:
                                self._override_method(api, method_path, module)

                if api == "cohere":
                    # Patch cohere v5.4.0+ methods
                    module_version = version(api)
                    if module_version is None:
                        logger.warning(
                            f"Cannot determine Cohere version. Only Cohere>=5.4.0 supported."
                        )

                    if Version(module_version) >= parse("5.4.0"):
                        self.override_cohere_chat()
                        self.override_cohere_chat_stream()
                    else:
                        logger.warning(
                            f"Only Cohere>=5.4.0 supported. v{module_version} found."
                        )

    def stop_instrumenting(self):
        """Restore original methods for all providers"""
        if 'openai' in self.active_providers:
            from openai.resources.chat import completions
            original = self._get_original_method('openai', 'create')
            if original:
                completions.Completions.create = original
            original_async = self._get_original_method('openai', 'create_async')
            if original_async:
                completions.AsyncCompletions.create = original_async
                
        if 'litellm' in self.active_providers:
            import litellm
            original = self._get_original_method('litellm', 'completion')
            if original:
                litellm.completion = original
            original_async = self._get_original_method('litellm', 'acompletion')
            if original_async:
                litellm.acompletion = original_async
                
        self.active_providers.clear()
        self.original_methods.clear()

    def undo_override_openai_v1_completion(self):
        global original_create
        from openai.resources.chat import completions

        completions.Completions.create = original_create

    def undo_override_openai_v1_async_completion(self):
        global original_create_async
        from openai.resources.chat import completions

        completions.AsyncCompletions.create = original_create_async

    def override_openai_v1_async_completion(self):
        from openai.resources.chat import completions
        
        # Store with provider context
        self._store_original_method('openai', 'create_async', completions.AsyncCompletions.create)
        self.active_providers['openai'] = True

        async def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()
            # Get provider-specific original method
            original = self._get_original_method('openai', 'create_async')
            result = await original(*args, **kwargs)
            return self._handle_response_v1_openai(result, kwargs, init_timestamp)

        completions.AsyncCompletions.create = patched_function
