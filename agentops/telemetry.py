from opentelemetry.trace.status import Status, StatusCode
import requests  # TODO: package this such that this is in an optional dependency
from functools import wraps
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, Span
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.trace import SpanKind


class RequestBodySpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context):
        # Check if the span is an HTTP client span
        if span.kind == SpanKind.CLIENT and "http.method" in span.attributes:
            # Attempt to retrieve the request body from the span context or attributes
            request_body = span.attributes.get("http.request.body")
            if request_body:
                # Add the request body as an attribute to the span
                span.set_attribute("http.request.body", request_body)

    def on_end(self, span: Span):
        # No action needed on span end for this example
        pass

    def shutdown(self):
        # Shutdown actions if necessary
        pass

    def force_flush(self, timeout_millis: int = 30000):
        # Force flushing actions if necessary
        pass


def initialize_tracing():
    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(RequestBodySpanProcessor())
    trace.get_tracer_provider().add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter())
    )


def initilize_request_tracing():
    RequestsInstrumentor().instrument()


def trace_request(func):
    """Decorator to trace HTTP requests made with the `requests` library."""

    @wraps(func)
    def wrapper_trace_request(*args, **kwargs):
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("agentops-patched-request-body", kind=trace.SpanKind.CLIENT) as span:
            if 'json' in kwargs:
                span.set_attribute("http.request.body", str(kwargs['json']))
            elif 'data' in kwargs:
                span.set_attribute("http.request.body", str(kwargs['data']))

            try:
                response = func(*args, **kwargs)
                return response
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, description=str(e)))
                raise

    return wrapper_trace_request


requests.post = trace_request(requests.post)
