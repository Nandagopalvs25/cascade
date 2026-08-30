import json
import logging
import sys

CLOUD_LOGGING_SEVERITY_BY_LEVEL = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

STANDARD_LOG_RECORD_ATTRIBUTES = frozenset(
    vars(
        logging.LogRecord(
            name="", level=logging.INFO, pathname="", lineno=0, msg="", args=(), exc_info=None
        )
    )
) | {"message", "asctime", "taskName"}


class CloudLoggingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": CLOUD_LOGGING_SEVERITY_BY_LEVEL.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        entry.update(
            {
                key: value
                for key, value in vars(record).items()
                if key not in STANDARD_LOG_RECORD_ATTRIBUTES
            }
        )
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_structured_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingJsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
