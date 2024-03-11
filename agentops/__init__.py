# agentops/__init__.py

from .client import Client
from .event import ActionEvent, ErrorEvent, LLMEvent, ToolEvent
from .logger import AgentOpsLogger
from .enums import Models
from .telemetry import initialize_tracing, initilize_request_tracing

initilize_request_tracing()
initialize_tracing()
