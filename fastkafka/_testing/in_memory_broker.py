# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/001_InMemoryBroker.ipynb.

# %% auto 0
__all__ = ['logger', 'KafkaRecord', 'KafkaPartition', 'KafkaTopic', 'split_list', 'GroupMetadata', 'InMemoryBroker',
           'InMemoryConsumer', 'InMemoryProducer']

# %% ../../nbs/001_InMemoryBroker.ipynb 1
import asyncio
import copy
import hashlib
import inspect
import random
import string
import uuid
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass
from typing import *

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import ConsumerRecord, RecordMetadata, TopicPartition

import fastkafka._application.app
import fastkafka._components.aiokafka_consumer_loop
import fastkafka._components.aiokafka_producer_manager
from .._components.logger import get_logger
from fastkafka._components.meta import (
    _get_default_kwargs_from_sig,
    classcontextmanager,
    copy_func,
    delegates,
    patch,
)

# %% ../../nbs/001_InMemoryBroker.ipynb 3
logger = get_logger(__name__)

# %% ../../nbs/001_InMemoryBroker.ipynb 6
@dataclass
class KafkaRecord:
    topic: str = ""
    partition: int = 0
    key: Optional[bytes] = None
    value: bytes = b""
    offset: int = 0

# %% ../../nbs/001_InMemoryBroker.ipynb 7
class KafkaPartition:
    def __init__(self, *, partition: int, topic: str):
        self.partition = partition
        self.topic = topic
        self.messages: List[KafkaRecord] = list()

    def write(self, value: bytes, key: Optional[bytes] = None) -> RecordMetadata:  # type: ignore
        record = KafkaRecord(
            topic=self.topic,
            partition=self.partition,
            value=value,
            key=key,
            offset=len(self.messages),
        )
        record_meta = RecordMetadata(
            topic=self.topic,
            partition=self.partition,
            topic_partition=TopicPartition(topic=self.topic, partition=self.partition),
            offset=len(self.messages),
            timestamp=1680602752070,
            timestamp_type=0,
            log_start_offset=0,
        )
        self.messages.append(record)
        return record_meta

    def read(self, offset: int) -> Tuple[List[KafkaRecord], int]:
        return self.messages[offset:], len(self.messages)

    def latest_offset(self) -> int:
        return len(self.messages)

# %% ../../nbs/001_InMemoryBroker.ipynb 11
class KafkaTopic:
    def __init__(self, topic: str, num_partitions: int = 1):
        self.topic = topic
        self.num_partitions = num_partitions
        self.partitions: List[KafkaPartition] = [
            KafkaPartition(topic=topic, partition=partition_index)
            for partition_index in range(num_partitions)
        ]

    def read(  # type: ignore
        self, partition: int, offset: int
    ) -> Tuple[TopicPartition, List[KafkaRecord], int]:
        topic_partition = TopicPartition(topic=self.topic, partition=partition)
        records, offset = self.partitions[partition].read(offset)
        return topic_partition, records, offset

    def write_with_partition(  # type: ignore
        self,
        value: bytes,
        partition: int,
    ) -> RecordMetadata:
        return self.partitions[partition].write(value)

    def write_with_key(self, value: bytes, key: bytes) -> RecordMetadata:  # type: ignore
        partition = int(hashlib.sha256(key).hexdigest(), 16) % self.num_partitions
        return self.partitions[partition].write(value, key=key)

    def write(  # type: ignore
        self,
        value: bytes,
        *,
        key: Optional[bytes] = None,
        partition: Optional[int] = None,
    ) -> RecordMetadata:
        if partition is not None:
            return self.write_with_partition(value, partition)

        if key is not None:
            return self.write_with_key(value, key)

        partition = random.randint(0, self.num_partitions - 1)  # nosec
        return self.write_with_partition(value, partition)

    def latest_offset(self, partition: int) -> int:
        return self.partitions[partition].latest_offset()

# %% ../../nbs/001_InMemoryBroker.ipynb 17
def split_list(list_to_split: List[Any], split_size: int) -> List[List[Any]]:
    return [
        list_to_split[start_index : start_index + split_size]
        for start_index in range(0, len(list_to_split), split_size)
    ]

# %% ../../nbs/001_InMemoryBroker.ipynb 19
class GroupMetadata:
    def __init__(self, num_partitions: int):
        self.num_partitions = num_partitions
        self.partitions_offsets: Dict[int, int] = {}
        self.consumer_ids: List[uuid.UUID] = list()
        self.partition_assignments: Dict[uuid.UUID, List[int]] = {}

    def subscribe(self, consumer_id: uuid.UUID) -> None:
        self.consumer_ids.append(consumer_id)
        self.rebalance()

    def unsubscribe(self, consumer_id: uuid.UUID) -> None:
        self.consumer_ids.remove(consumer_id)
        self.rebalance()

    def rebalance(self) -> None:
        if len(self.consumer_ids) == 0:
            self.partition_assignments = {}
        else:
            partitions_per_actor = self.num_partitions // len(self.consumer_ids)
            if self.num_partitions % len(self.consumer_ids) != 0:
                partitions_per_actor += 1
            self.assign_partitions(partitions_per_actor)

    def assign_partitions(self, partitions_per_actor: int) -> None:
        partitions = [i for i in range(self.num_partitions)]

        partitions_split = split_list(partitions, partitions_per_actor)
        self.partition_assignments = {
            self.consumer_ids[i]: partition_split
            for i, partition_split in enumerate(partitions_split)
        }

    def get_partitions(
        self, consumer_id: uuid.UUID
    ) -> Tuple[List[int], Dict[int, Optional[int]]]:
        partition_assignments = self.partition_assignments.get(consumer_id, [])
        partition_offsets_assignments = {
            partition: self.partitions_offsets.get(partition, None)
            for partition in partition_assignments
        }
        return partition_assignments, partition_offsets_assignments

    def set_offset(self, partition: int, offset: int) -> None:
        self.partitions_offsets[partition] = offset

# %% ../../nbs/001_InMemoryBroker.ipynb 22
@classcontextmanager()
class InMemoryBroker:
    def __init__(
        self,
        num_partitions: int = 1,
    ):
        self.num_partitions = num_partitions
        self.topics: Dict[Tuple[str, str], KafkaTopic] = {}
        self.topic_groups: Dict[Tuple[str, str, str], GroupMetadata] = {}
        self.is_started: bool = False

    def connect(self) -> uuid.UUID:
        return uuid.uuid4()

    def dissconnect(self, consumer_id: uuid.UUID) -> None:
        pass

    def subscribe(
        self, bootstrap_server: str, topic: str, group: str, consumer_id: uuid.UUID
    ) -> None:
        raise NotImplementedError()

    def unsubscribe(
        self, bootstrap_server: str, topic: str, group: str, consumer_id: uuid.UUID
    ) -> None:
        raise NotImplementedError()

    def read(  # type: ignore
        self,
        *,
        bootstrap_server: str,
        topic: str,
        group: str,
        consumer_id: uuid.UUID,
        auto_offset_reset: str,
    ) -> Dict[TopicPartition, List[KafkaRecord]]:
        raise NotImplementedError()

    def write(  # type: ignore
        self,
        *,
        bootstrap_server: str,
        topic: str,
        value: bytes,
        key: Optional[bytes] = None,
        partition: Optional[int] = None,
    ) -> RecordMetadata:
        raise NotImplementedError()

    @contextmanager
    def lifecycle(self) -> Iterator["InMemoryBroker"]:
        raise NotImplementedError()

    async def _start(self) -> str:
        logger.info("InMemoryBroker._start() called")
        self.__enter__()  # type: ignore
        return "localbroker:0"

    async def _stop(self) -> None:
        logger.info("InMemoryBroker._stop() called")
        self.__exit__(None, None, None)  # type: ignore

# %% ../../nbs/001_InMemoryBroker.ipynb 23
@patch
def subscribe(
    self: InMemoryBroker,
    bootstrap_server: str,
    topic: str,
    group: str,
    consumer_id: uuid.UUID,
) -> None:
    if (bootstrap_server, topic) not in self.topics:
        self.topics[(bootstrap_server, topic)] = KafkaTopic(
            topic=topic, num_partitions=self.num_partitions
        )

    group_meta = self.topic_groups.get(
        (bootstrap_server, topic, group), GroupMetadata(self.num_partitions)
    )
    group_meta.subscribe(consumer_id)
    self.topic_groups[(bootstrap_server, topic, group)] = group_meta


@patch
def unsubscribe(
    self: InMemoryBroker,
    bootstrap_server: str,
    topic: str,
    group: str,
    consumer_id: uuid.UUID,
) -> None:
    self.topic_groups[(bootstrap_server, topic, group)].unsubscribe(consumer_id)

# %% ../../nbs/001_InMemoryBroker.ipynb 25
@patch
def write(  # type: ignore
    self: InMemoryBroker,
    *,
    bootstrap_server: str,
    topic: str,
    value: bytes,
    key: Optional[bytes] = None,
    partition: Optional[int] = None,
) -> RecordMetadata:
    if (bootstrap_server, topic) not in self.topics:
        self.topics[(bootstrap_server, topic)] = KafkaTopic(
            topic=topic, num_partitions=self.num_partitions
        )

    return self.topics[(bootstrap_server, topic)].write(
        value, key=key, partition=partition
    )

# %% ../../nbs/001_InMemoryBroker.ipynb 27
@patch
def read(  # type: ignore
    self: InMemoryBroker,
    *,
    bootstrap_server: str,
    topic: str,
    group: str,
    consumer_id: uuid.UUID,
    auto_offset_reset: str,
) -> Dict[TopicPartition, List[KafkaRecord]]:
    group_meta = self.topic_groups[(bootstrap_server, topic, group)]
    partitions, offsets = group_meta.get_partitions(consumer_id)

    if len(partitions) == 0:
        return {}

    partitions_data = {}

    for partition in partitions:
        offset = offsets[partition]

        if offset is None:
            offset = (
                self.topics[(bootstrap_server, topic)].latest_offset(partition)
                if auto_offset_reset == "latest"
                else 0
            )

        topic_partition, data, offset = self.topics[(bootstrap_server, topic)].read(
            partition, offset
        )

        partitions_data[topic_partition] = data
        group_meta.set_offset(partition, offset)

    return partitions_data

# %% ../../nbs/001_InMemoryBroker.ipynb 34
# InMemoryConsumer
class InMemoryConsumer:
    def __init__(
        self,
        broker: InMemoryBroker,
    ) -> None:
        self.broker = broker
        self._id: Optional[uuid.UUID] = None
        self._auto_offset_reset: str = "latest"
        self._group_id: Optional[str] = None
        self._topics: List[str] = list()
        self._bootstrap_servers = ""

    @delegates(AIOKafkaConsumer)
    def __call__(self, **kwargs: Any) -> "InMemoryConsumer":
        defaults = _get_default_kwargs_from_sig(InMemoryConsumer.__call__, **kwargs)
        consume_copy = InMemoryConsumer(self.broker)
        consume_copy._auto_offset_reset = defaults["auto_offset_reset"]
        consume_copy._bootstrap_servers = defaults["bootstrap_servers"]
        consume_copy._group_id = (
            defaults["group_id"]
            if defaults["group_id"] is not None
            else "".join(random.choices(string.ascii_letters, k=10))  # nosec
        )
        return consume_copy

    @delegates(AIOKafkaConsumer.start)
    async def start(self, **kwargs: Any) -> None:
        pass

    @delegates(AIOKafkaConsumer.stop)
    async def stop(self, **kwargs: Any) -> None:
        pass

    @delegates(AIOKafkaConsumer.subscribe)
    def subscribe(self, topics: List[str], **kwargs: Any) -> None:
        raise NotImplementedError()

    @delegates(AIOKafkaConsumer.getmany)
    async def getmany(  # type: ignore
        self, **kwargs: Any
    ) -> Dict[TopicPartition, List[ConsumerRecord]]:
        raise NotImplementedError()

# %% ../../nbs/001_InMemoryBroker.ipynb 37
@patch
@delegates(AIOKafkaConsumer.start)
async def start(self: InMemoryConsumer, **kwargs: Any) -> None:
    logger.info("AIOKafkaConsumer patched start() called()")
    if self._id is not None:
        raise RuntimeError(
            "Consumer start() already called! Run consumer stop() before running start() again"
        )
    self._id = self.broker.connect()

# %% ../../nbs/001_InMemoryBroker.ipynb 40
@patch  # type: ignore
@delegates(AIOKafkaConsumer.subscribe)
def subscribe(self: InMemoryConsumer, topics: List[str], **kwargs: Any) -> None:
    logger.info("AIOKafkaConsumer patched subscribe() called")
    if self._id is None:
        raise RuntimeError("Consumer start() not called! Run consumer start() first")
    logger.info(f"AIOKafkaConsumer.subscribe(), subscribing to: {topics}")
    for topic in topics:
        self.broker.subscribe(
            bootstrap_server=self._bootstrap_servers,
            consumer_id=self._id,
            topic=topic,
            group=self._group_id,  # type: ignore
        )
        self._topics.append(topic)

# %% ../../nbs/001_InMemoryBroker.ipynb 43
@patch
@delegates(AIOKafkaConsumer.stop)
async def stop(self: InMemoryConsumer, **kwargs: Any) -> None:
    logger.info("AIOKafkaConsumer patched stop() called")
    if self._id is None:
        raise RuntimeError("Consumer start() not called! Run consumer start() first")
    for topic in self._topics:
        self.broker.unsubscribe(
            bootstrap_server=self._bootstrap_servers,
            topic=topic,
            group=self._group_id,  # type: ignore
            consumer_id=self._id,
        )

# %% ../../nbs/001_InMemoryBroker.ipynb 46
@patch
@delegates(AIOKafkaConsumer.getmany)
async def getmany(  # type: ignore
    self: InMemoryConsumer, **kwargs: Any
) -> Dict[TopicPartition, List[ConsumerRecord]]:
    for topic in self._topics:
        return self.broker.read(
            bootstrap_server=self._bootstrap_servers,
            topic=topic,
            consumer_id=self._id,  # type: ignore
            group=self._group_id,  # type: ignore
            auto_offset_reset=self._auto_offset_reset,
        )

# %% ../../nbs/001_InMemoryBroker.ipynb 49
class InMemoryProducer:
    def __init__(self, broker: InMemoryBroker, **kwargs: Any) -> None:
        self.broker = broker
        self.id: Optional[uuid.UUID] = None
        self._bootstrap_servers = ""

    @delegates(AIOKafkaProducer)
    def __call__(self, **kwargs: Any) -> "InMemoryProducer":
        defaults = _get_default_kwargs_from_sig(InMemoryConsumer.__call__, **kwargs)
        producer_copy = InMemoryProducer(self.broker)
        producer_copy._bootstrap_servers = defaults["bootstrap_servers"]
        return producer_copy

    @delegates(AIOKafkaProducer.start)
    async def start(self, **kwargs: Any) -> None:
        raise NotImplementedError()

    @delegates(AIOKafkaProducer.stop)
    async def stop(self, **kwargs: Any) -> None:
        raise NotImplementedError()

    @delegates(AIOKafkaProducer.send)
    async def send(  # type: ignore
        self: AIOKafkaProducer,
        topic: str,
        msg: bytes,
        key: Optional[bytes] = None,
        **kwargs: Any,
    ):
        raise NotImplementedError()

# %% ../../nbs/001_InMemoryBroker.ipynb 52
@patch  # type: ignore
@delegates(AIOKafkaProducer.start)
async def start(self: InMemoryProducer, **kwargs: Any) -> None:
    logger.info("AIOKafkaProducer patched start() called()")
    if self.id is not None:
        raise RuntimeError(
            "Producer start() already called! Run producer stop() before running start() again"
        )
    self.id = self.broker.connect()

# %% ../../nbs/001_InMemoryBroker.ipynb 55
@patch  # type: ignore
@delegates(AIOKafkaProducer.stop)
async def stop(self: InMemoryProducer, **kwargs: Any) -> None:
    logger.info("AIOKafkaProducer patched stop() called")
    if self.id is None:
        raise RuntimeError("Producer start() not called! Run producer start() first")

# %% ../../nbs/001_InMemoryBroker.ipynb 58
@patch
@delegates(AIOKafkaProducer.send)
async def send(  # type: ignore
    self: InMemoryProducer,
    topic: str,
    msg: bytes,
    key: Optional[bytes] = None,
    partition: Optional[int] = None,
    **kwargs: Any,
):  # asyncio.Task[RecordMetadata]
    if self.id is None:
        raise RuntimeError("Producer start() not called! Run producer start() first")

    record = self.broker.write(
        bootstrap_server=self._bootstrap_servers,
        topic=topic,
        value=msg,
        key=key,
        partition=partition,
    )

    async def _f(record: ConsumerRecord = record) -> RecordMetadata:  # type: ignore
        return record

    return asyncio.create_task(_f())

# %% ../../nbs/001_InMemoryBroker.ipynb 61
@patch
@contextmanager
def lifecycle(self: InMemoryBroker) -> Iterator[InMemoryBroker]:
    logger.info(
        "InMemoryBroker._patch_consumers_and_producers(): Patching consumers and producers!"
    )
    try:
        logger.info("InMemoryBroker starting")

        old_consumer_app = fastkafka._application.app.AIOKafkaConsumer
        old_producer_app = fastkafka._application.app.AIOKafkaProducer
        old_consumer_loop = (
            fastkafka._components.aiokafka_consumer_loop.AIOKafkaConsumer
        )
        old_producer_manager = (
            fastkafka._components.aiokafka_producer_manager.AIOKafkaProducer
        )

        fastkafka._application.app.AIOKafkaConsumer = InMemoryConsumer(self)
        fastkafka._application.app.AIOKafkaProducer = InMemoryProducer(self)
        fastkafka._components.aiokafka_consumer_loop.AIOKafkaConsumer = (
            InMemoryConsumer(self)
        )
        fastkafka._components.aiokafka_producer_manager.AIOKafkaProducer = (
            InMemoryProducer(self)
        )

        self.is_started = True
        yield self
    finally:
        logger.info("InMemoryBroker stopping")

        fastkafka._application.app.AIOKafkaConsumer = old_consumer_app
        fastkafka._application.app.AIOKafkaProducer = old_producer_app
        fastkafka._components.aiokafka_consumer_loop.AIOKafkaConsumer = (
            old_consumer_loop
        )
        fastkafka._components.aiokafka_producer_manager.AIOKafkaProducer = (
            old_producer_manager
        )

        self.is_started = False
