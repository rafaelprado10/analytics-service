import os
import sys
import threading
import json
import uuid
import time
import logging
from contextvars import ContextVar
from datetime import datetime

import boto3
from botocore.exceptions import NoCredentialsError, ClientError
from flask import Flask, request, jsonify, g
from dotenv import load_dotenv
from opentelemetry import trace

from telemetry import setup_telemetry, instrument_flask, get_tracer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "analytics-service")
_worker_message_id: ContextVar[str] = ContextVar("worker_message_id", default="-")
_worker_trace_id: ContextVar[str] = ContextVar("worker_trace_id", default="-")


def _request_context():
    try:
        return (
            g.message_id,
            getattr(g, "trace_id", "-"),
            datetime.now().isoformat(),
        )
    except RuntimeError:
        return (
            _worker_message_id.get(),
            _worker_trace_id.get(),
            datetime.now().isoformat(),
        )


def _log_line(message: str, level: str = "info") -> None:
    msg_id, trace_id, ts = _request_context()
    line = f" {ts} | {msg_id} | {trace_id} | {SERVICE_NAME} | {message}"
    getattr(log, level)(line)


def log_info(message: str) -> None:
    _log_line(message, "info")


def log_warning(message: str) -> None:
    _log_line(message, "warning")


def log_error(message: str) -> None:
    _log_line(message, "error")


def log_critical(message: str) -> None:
    _log_line(message, "critical")


load_dotenv()
setup_telemetry()

AWS_REGION = os.getenv("AWS_REGION")
SQS_QUEUE_URL = os.getenv("AWS_SQS_URL")
DYNAMODB_TABLE_NAME = os.getenv("AWS_DYNAMODB_TABLE")

if not all([AWS_REGION, SQS_QUEUE_URL, DYNAMODB_TABLE_NAME]):
    log_critical(
        "AWS_REGION, AWS_SQS_URL e AWS_DYNAMODB_TABLE devem ser definidos"
    )
    sys.exit(1)

try:
    session = boto3.Session(region_name=AWS_REGION)
    sqs_client = session.client("sqs")
    dynamodb_client = session.client("dynamodb")
    log_info(f"Clientes Boto3 inicializados | region={AWS_REGION}")
except NoCredentialsError:
    log_critical("Credenciais AWS não encontradas")
    sys.exit(1)
except Exception as e:
    log_critical(f"Erro ao inicializar Boto3: {e}")
    sys.exit(1)

tracer = get_tracer()


def process_message(message):
    sqs_id = message["MessageId"]
    token_mid = _worker_message_id.set(sqs_id)

    with tracer.start_as_current_span("sqs.process_message") as span:
        span_ctx = span.get_span_context()
        trace_id = (
            format(span_ctx.trace_id, "032x") if span_ctx.trace_id else "-"
        )
        token_tid = _worker_trace_id.set(trace_id)
        span.set_attribute("messaging.message_id", sqs_id)

        try:
            log_info(f"SQS worker | processando mensagem | sqs_message_id={sqs_id}")
            body = json.loads(message["Body"])

            event_id = str(uuid.uuid4())
            flag_name = body.get("flag_name", "")
            span.set_attribute("analytics.event_id", event_id)
            span.set_attribute("flag.name", flag_name)

            item = {
                "event_id": {"S": event_id},
                "user_id": {"S": body["user_id"]},
                "flag_name": {"S": flag_name},
                "result": {"BOOL": body["result"]},
                "timestamp": {"S": body["timestamp"]},
            }

            log_info(
                f"SQS worker | PutItem DynamoDB | table={DYNAMODB_TABLE_NAME} | "
                f"event_id={event_id}"
            )
            dynamodb_client.put_item(TableName=DYNAMODB_TABLE_NAME, Item=item)

            sqs_client.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=message["ReceiptHandle"],
            )
            log_info(
                f"SQS worker | evento salvo e mensagem removida da fila | "
                f"event_id={event_id} | flag_name={flag_name}"
            )

        except json.JSONDecodeError:
            span.set_attribute("error", True)
            log_error(
                f"SQS worker | JSON inválido | sqs_message_id={sqs_id}"
            )
        except ClientError as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            log_error(
                f"SQS worker | erro Boto3 | sqs_message_id={sqs_id}: {e}"
            )
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            log_error(
                f"SQS worker | erro inesperado | sqs_message_id={sqs_id}: {e}"
            )
        finally:
            _worker_trace_id.reset(token_tid)

    _worker_message_id.reset(token_mid)


def sqs_worker_loop():
    log_info("SQS worker | iniciando loop de consumo")
    while True:
        try:
            log_info("SQS worker | aguardando mensagens (long poll 20s)")
            response = sqs_client.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20,
            )

            messages = response.get("Messages", [])
            if not messages:
                continue

            log_info(f"SQS worker | recebidas {len(messages)} mensagens")
            for message in messages:
                process_message(message)

        except ClientError as e:
            log_error(f"SQS worker | erro Boto3 no loop principal: {e}")
            time.sleep(10)
        except Exception as e:
            log_error(f"SQS worker | erro inesperado no loop principal: {e}")
            time.sleep(10)


app = Flask(__name__)


@app.before_request
def create_request_context():
    incoming_request_id = request.headers.get("X-Request-ID")
    g.message_id = incoming_request_id or str(uuid.uuid4())
    g.start_time = datetime.now()

    span = trace.get_current_span()
    span_ctx = span.get_span_context()
    g.trace_id = (
        format(span_ctx.trace_id, "032x") if span_ctx.trace_id else "-"
    )

    log_info(f"{request.method} {request.path} | início da requisição")


@app.after_request
def add_response_headers(response):
    response.headers["X-Request-ID"] = g.message_id
    elapsed_ms = int(
        (datetime.now() - g.start_time).total_seconds() * 1000
    )
    log_info(
        f"{request.method} {request.path} | status={response.status_code} | "
        f"elapsed_ms={elapsed_ms}"
    )
    return response


instrument_flask(app)


@app.route("/health")
def health():
    log_info("GET /health | health check")
    return jsonify({"status": "ok"})


def start_worker():
    worker_thread = threading.Thread(target=sqs_worker_loop, daemon=True)
    worker_thread.start()
    log_info("SQS worker | thread de background iniciada")


start_worker()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8005))
    app.run(host="0.0.0.0", port=port, debug=False)
