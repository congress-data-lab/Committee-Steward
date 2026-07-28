"""Events package."""

from .crec_parser import parse_crec_text
from .hres_parser import parse_hres_xml
from .sres_parser import parse_senate_appointment_text, parse_sres_xml
from .senate_journal_parser import (
    parse_senate_journal_file,
    detect_actions_on_page_senate,
    passes_quality_gates_senate,
    get_gpo_senate_journal_files,
)

__all__ = [
    "parse_crec_text",
    "parse_hres_xml",
    "parse_sres_xml",
    "parse_senate_appointment_text",
    "parse_senate_journal_file",
    "detect_actions_on_page_senate",
    "passes_quality_gates_senate",
    "get_gpo_senate_journal_files",
]
