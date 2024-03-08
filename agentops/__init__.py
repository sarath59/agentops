# agentops/__init__.py

from .client import Client
from .event import ActionEvent, ErrorEvent, LLMEvent, ToolEvent
from .logger import AgentOpsLogger
from .enums import Models

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor


def initialize_tracing():
    trace.set_tracer_provider(TracerProvider())
    trace.get_tracer_provider().add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter())
    )


RequestsInstrumentor().instrument()
initialize_tracing()
