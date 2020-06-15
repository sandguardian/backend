from unittest.mock import Mock, call
from uuid import uuid4

import pendulum
import pytest


@pytest.fixture
def user1(user_manager, cognito_client):
    user_id, username = str(uuid4()), str(uuid4())[:8]
    cognito_client.create_verified_user_pool_entry(user_id, username, f'{username}@real.app')
    yield user_manager.create_cognito_only_user(user_id, username)


user2 = user1


@pytest.fixture
def chat(chat_manager, user1, user2):
    yield chat_manager.add_direct_chat('cid', user1.id, user2.id)


@pytest.fixture
def message(chat_message_manager, chat, user1):
    yield chat_message_manager.add_chat_message(str(uuid4()), 'lore ipsum', chat.id, user1.id)


@pytest.fixture
def system_message(chat_message_manager, chat):
    yield chat_message_manager.add_system_message(chat.id, 'lore ipsum')


def test_postprocess_chat_message_added(chat_message_manager, message):
    typed_pk = chat_message_manager.dynamo.typed_pk(message.id)
    pk, sk = typed_pk['partitionKey']['S'], typed_pk['sortKey']['S']
    old_item = None
    new_item = chat_message_manager.dynamo.client.get_typed_item(typed_pk)
    created_at = pendulum.parse(message.item['createdAt'])

    # postprocess the user message, verify calls correct
    chat_message_manager.chat_manager = Mock(chat_message_manager.chat_manager)
    chat_message_manager.postprocess_record(pk, sk, old_item, new_item)
    assert chat_message_manager.chat_manager.mock_calls == [
        call.postprocess_chat_message_added(message.chat_id, message.user_id, created_at),
    ]


def test_postprocess_system_chat_message_added(chat_message_manager, system_message):
    typed_pk = chat_message_manager.dynamo.typed_pk(system_message.id)
    pk, sk = typed_pk['partitionKey']['S'], typed_pk['sortKey']['S']
    old_item = None
    new_item = chat_message_manager.dynamo.client.get_typed_item(typed_pk)
    created_at = pendulum.parse(system_message.item['createdAt'])

    # postprocess the user message, verify calls correct
    chat_message_manager.chat_manager = Mock(chat_message_manager.chat_manager)
    chat_message_manager.postprocess_record(pk, sk, old_item, new_item)
    assert chat_message_manager.chat_manager.mock_calls == [
        call.postprocess_chat_message_added(system_message.chat_id, None, created_at),
    ]


def test_postprocess_chat_message_deleted(chat_message_manager, message):
    typed_pk = chat_message_manager.dynamo.typed_pk(message.id)
    pk, sk = typed_pk['partitionKey']['S'], typed_pk['sortKey']['S']
    old_item = chat_message_manager.dynamo.client.get_typed_item(typed_pk)
    new_item = None

    # postprocess the user message, verify calls correct
    chat_message_manager.chat_manager = Mock(chat_message_manager.chat_manager)
    chat_message_manager.postprocess_record(pk, sk, old_item, new_item)
    assert chat_message_manager.chat_manager.mock_calls == [
        call.postprocess_chat_message_deleted(message.chat_id, message.id, message.user_id),
    ]


def test_postprocess_chat_message_view_added(chat_message_manager, message, user2):
    # create a view by user2
    message.view_dynamo.add_view(message.id, user2.id, 1, pendulum.now('utc'))
    typed_pk = message.view_dynamo.typed_pk(message.id, user2.id)
    pk, sk = typed_pk['partitionKey']['S'], typed_pk['sortKey']['S']
    old_item = None
    new_item = message.view_dynamo.client.get_typed_item(typed_pk)

    # postprocess adding that message view, verify calls correct
    chat_message_manager.chat_manager = Mock(chat_message_manager.chat_manager)
    chat_message_manager.postprocess_record(pk, sk, old_item, new_item)
    assert chat_message_manager.chat_manager.mock_calls == [
        call.postprocess_chat_message_view_added(message.chat_id, user2.id),
    ]