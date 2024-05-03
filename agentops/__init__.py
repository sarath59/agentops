# agentops/__init__.py
from os import environ
from typing import Optional, List

from .client import Client
from .config import Configuration
from .event import Event, ErrorEvent
from .log_config import set_logging_level_info

import sys
from importlib import import_module
from importlib.metadata import version
from packaging.version import Version, parse
import logger
from .llm_tracker import override_openai_v1_completion, override_litellm_async_completion, override_openai_v1_async_completion, _override_method


def init(api_key: Optional[str] = None,
         parent_key: Optional[str] = None,
         endpoint: Optional[str] = None,
         max_wait_time: Optional[int] = None,
         max_queue_size: Optional[int] = None,
         tags: Optional[List[str]] = None,
         override: Optional[bool] = None,  # Deprecated
         instrument_llm_calls=True,
         auto_start_session=True,
         inherited_session_id: Optional[str] = None
         ):
    """
        Initializes the AgentOps singleton pattern.

        Args:

            api_key (str, optional): API Key for AgentOps services. If none is provided, key will
                be read from the AGENTOPS_API_KEY environment variable.
            parent_key (str, optional): Organization key to give visibility of all user sessions the user's organization. If none is provided, key will
                be read from the AGENTOPS_PARENT_KEY environment variable.
            endpoint (str, optional): The endpoint for the AgentOps service. If none is provided, key will
                be read from the AGENTOPS_API_ENDPOINT environment variable. Defaults to 'https://api.agentops.ai'.
            max_wait_time (int, optional): The maximum time to wait in milliseconds before flushing the queue.
                Defaults to 30,000 (30 seconds)
            max_queue_size (int, optional): The maximum size of the event queue. Defaults to 100.
            tags (List[str], optional): Tags for the sessions that can be used for grouping or
                sorting later (e.g. ["GPT-4"]).
            override (bool, optional): [Deprecated] Use `instrument_llm_calls` instead. Whether to instrument LLM calls and emit LLMEvents..
            instrument_llm_calls (bool): Whether to instrument LLM calls and emit LLMEvents..
            auto_start_session (bool): Whether to start a session automatically when the client is created.
            inherited_session_id (optional, str): Init Agentops with an existing Session
        Attributes:
    """
    set_logging_level_info()
    c = Client(api_key=api_key,
               parent_key=parent_key,
               endpoint=endpoint,
               max_wait_time=max_wait_time,
               max_queue_size=max_queue_size,
               tags=tags,
               override=override,
               instrument_llm_calls=instrument_llm_calls,
               auto_start_session=auto_start_session,
               inherited_session_id=inherited_session_id
               )

    return inherited_session_id or c.current_session_id


def end_session(end_state: str,
                end_state_reason: Optional[str] = None,
                video: Optional[str] = None):
    """
        End the current session with the AgentOps service.

        Args:
            end_state (str): The final state of the session. Options: Success, Fail, or Indeterminate.
            end_state_reason (str, optional): The reason for ending the session.
            video (str, optional): URL to a video recording of the session
    """
    Client().end_session(end_state, end_state_reason, video)


def start_session(tags: Optional[List[str]] = None, config: Optional[Configuration] = None, inherited_session_id: Optional[str] = None):
    """
        Start a new session for recording events.

        Args:
            tags (List[str], optional): Tags that can be used for grouping or sorting later.
                e.g. ["test_run"].
            config: (Configuration, optional): Client configuration object
    """
    return Client().start_session(tags, config, inherited_session_id)


def record(event: Event | ErrorEvent):
    """
        Record an event with the AgentOps service.

        Args:
            event (Event): The event to record.
    """
    Client().record(event)


def add_tags(tags: List[str]):
    """
        Append to session tags at runtime. 

        Args:
            tags (List[str]): The list of tags to append.
    """
    Client().add_tags(tags)


def set_tags(tags: List[str]):
    """
        Replace session tags at runtime. 

        Args:
            tags (List[str]): The list of tags to set.
    """
    Client().set_tags(tags)


def get_api_key() -> str:
    return Client().api_key


def set_parent_key(parent_key):
    """
        Set the parent API key which has visibility to projects it is parent to.

        Args:
            parent_key (str): The API key of the parent organization to set.
    """
    Client().set_parent_key(parent_key)


def override_api(self):
    """
    Overrides key methods of the specified API to record events.
    """
    SUPPORTED_APIS = {
        'litellm': {'1.3.1': ("openai_chat_completions.completion",)},
        'openai': {
            '1.0.0': (
                "chat.completions.create",
            ),
            '0.0.0':
            (
                "ChatCompletion.create",
                "ChatCompletion.acreate",
            ),
        }
    }

    for api in SUPPORTED_APIS:
        if api in sys.modules:
            module = import_module(api)
            if api == 'litellm':
                module_version = version(api)
                if Version(module_version) >= parse('1.3.1'):
                    override_litellm_completion()
                    override_litellm_async_completion()
                else:
                    logger.warning(f'🖇 AgentOps: Only litellm>=1.3.1 supported. v{module_version} found.')
                return  # If using an abstraction like litellm, do not patch the underlying LLM APIs

            if api == 'openai':
                # Patch openai v1.0.0+ methods
                if hasattr(module, '__version__'):
                    module_version = parse(module.__version__)
                    if module_version >= parse('1.0.0'):
                        override_openai_v1_completion()
                        override_openai_v1_async_completion()
                    else:
                        # Patch openai <v1.0.0 methods
                        for method_path in SUPPORTED_APIS['openai']['0.0.0']:
                            _override_method(api, method_path, module)
