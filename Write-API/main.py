from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import HTTPException
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator
import uvicorn
import aio_pika
import logging
import sys
import asyncio
import json
import logging
import random
import uuid

from settings import settings  
from models import example
from schemas.message import Message
from enums.state import State

from google.protobuf.json_format import MessageToJson
import grpc
from todo_pb2_grpc import TodoServiceStub
from todo_pb2 import TodoRequest, TodoListResponse
from google.protobuf import timestamp_pb2

ch = logging.StreamHandler(sys.stdout)
logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[ch]
    )
MSG_LOG = dict()
PREFECTH_COUNT = 100
logger = logging.getLogger()
channel = grpc.insecure_channel("database-driver:50051")
stub = TodoServiceStub(channel)



async def get_amqp_connection() -> aio_pika.abc.AbstractConnection:
    """Connect to AMQP server."""
    return await aio_pika.connect_robust(str(settings.AMQP_URL))


async def declare_queue(
    channel: aio_pika.abc.AbstractChannel,
    queue: str,
    **kwargs,
) -> aio_pika.abc.AbstractQueue:
    """Create AMQP queue."""
    return await channel.declare_queue(name=queue, auto_delete=True, **kwargs)


async def get_channel(
    connection: aio_pika.abc.AbstractConnection = Depends(get_amqp_connection)
) -> AsyncGenerator[aio_pika.abc.AbstractChannel, None]:
    """Connect to and yield a AMQP channel.

    :yield: RabbitMQ channel.
    """
    async with connection:
        channel = await connection.channel()
        await declare_queue(channel=channel, queue=settings.QUEUE)
        yield channel


async def process_message(message: aio_pika.abc.AbstractIncomingMessage):
    """Do something with the message.
w
    :param message: A message from the queue.
    """
    try:
        async with message.process(requeue=True):
            logger.info(f"MESSAGE RECEIVED: {message.message_id}")
            msg = Message(**json.loads(message.body.decode()))
            MSG_LOG[message.message_id].update(
                state=State.RECEIVED, received_at=datetime.now()
            )
            duration = random.randint(1, 10)
            await asyncio.sleep(duration)
            logger.info(
                f"MESSAGE CONSUMED: {message.message_id} -- {msg.body} (duration {duration})"
            )
            MSG_LOG[message.message_id].update(
                state=State.CONSUMED,
                consumed_at=datetime.now(),
                duration=duration,
            )
    except Exception as e:
        logger.error(e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start internal message consumer on app startup."""
    connection = await aio_pika.connect_robust(str(settings.AMQP_URL))

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=PREFECTH_COUNT)
        queue = await declare_queue(channel=channel, queue=settings.QUEUE)
        # await queue.consume(process_message)
        yield

app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def logging_init(): 
    logging.info(f"Starting producer...")
    
    
@app.get("/")
async def read_root():
	return JSONResponse(content={"Hello":"World"})

@app.get("/healthcheck")
def healthcheck():
    """
    Check the health of the application.
    """
    return JSONResponse(content={"status": "ok"})

@app.get("/log")
def _():
    return MSG_LOG


async def publish_message(
    message: str,
    channel: aio_pika.abc.AbstractChannel,
):
    """Publish a message to the event queue.

    :param message: A message to publish.
    :param channel: The AMQP channel to publish the message to.
    """
    msg = aio_pika.Message(
        body=Message(body=message).model_dump_json().encode(),
        message_id=str(uuid.uuid4()),
    )
    await channel.default_exchange.publish(
        msg,
        routing_key=settings.QUEUE,
    )

    return msg


@app.get(
    "/publish",
    status_code=status.HTTP_202_ACCEPTED,
    description="Publish a message to the event queue.",
)
async def _(
    message: str = "Hello world!",
    channel: aio_pika.abc.AbstractChannel = Depends(get_channel),
):
    """Publish the provided message to the event queue.

    :param message: A message to publish, defaults to "Hello world!".
    :param channel: The AMQP channel to publish the message to
    (provided via `Depends(get_channel)`).
    """
    msg = await publish_message(channel=channel, message=message)
    MSG_LOG[msg.message_id] = dict(
        message=message,
        state=State.PUBLISHED,
        published_at=datetime.now(),
    )

    return {
        "status": "OK",
        "details": {
            "body": message,
            "event_id": msg.message_id,
        },
    }


@app.put("/update")
async def _(message_id: str, state: State):
    try:
        MSG_LOG[message_id].update(
            **{
                "state": state,
                f"{state.lower()}_at": datetime.now(),
            }
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Message ID {message_id} not found!",
        )

@app.post("/posts")
async def create_todo(title: str, description: str, channel: aio_pika.abc.AbstractChannel = Depends(get_channel),):
    # Create a timestamp for the current time
    current_time = datetime.utcnow()
    timestamp = timestamp_pb2.Timestamp()
    timestamp.FromDatetime(current_time)
    
    # Create a new Todo using the gRPC stub
    try:
        response = stub.Create(TodoRequest(
            title=title,
            description=description,
            created_at=timestamp
        ))
        # Convert the gRPC response message to JSON and return it
        msg = await publish_message(channel=channel, message=str(current_time))
        MSG_LOG[msg.message_id] = dict(
            message=str(current_time),
            state=State.PUBLISHED,
            published_at=datetime.utcnow(),
        )

        return {
            "status": "OK",
            "details": {
                "body": str(current_time),
                "event_id": msg.message_id,
            },
        }
    except grpc.RpcError as e:
        # Handle gRPC errors
        raise HTTPException(status_code=500, detail=f"gRPC error: {e.details()}")









if __name__ == '__main__':
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info", reload=True)