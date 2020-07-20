import collections
import logging

import pendulum

from app import models
from app.mixins.base import ManagerBase
from app.mixins.flag.manager import FlagManagerMixin
from app.mixins.view.manager import ViewManagerMixin

from .dynamo import ChatDynamo, ChatMemberDynamo
from .enums import ChatType
from .exceptions import ChatException
from .model import Chat

logger = logging.getLogger()


class ChatManager(FlagManagerMixin, ViewManagerMixin, ManagerBase):

    item_type = 'chat'

    def __init__(self, clients, managers=None):
        super().__init__(clients, managers=managers)
        managers = managers or {}
        managers['chat'] = self
        self.block_manager = managers.get('block') or models.BlockManager(clients, managers=managers)
        self.card_manager = managers.get('card') or models.CardManager(clients, managers=managers)
        self.chat_message_manager = managers.get('chat_message') or models.ChatMessageManager(
            clients, managers=managers
        )
        self.user_manager = managers.get('user') or models.UserManager(clients, managers=managers)

        self.clients = clients
        if 'dynamo' in clients:
            self.dynamo = ChatDynamo(clients['dynamo'])
            self.member_dynamo = ChatMemberDynamo(clients['dynamo'])

    def get_model(self, item_id, strongly_consistent=False):
        return self.get_chat(item_id, strongly_consistent=strongly_consistent)

    def get_chat(self, chat_id, strongly_consistent=False):
        item = self.dynamo.get(chat_id, strongly_consistent=strongly_consistent)
        return self.init_chat(item) if item else None

    def get_direct_chat(self, user_id_1, user_id_2):
        item = self.dynamo.get_direct_chat(user_id_1, user_id_2)
        return self.init_chat(item) if item else None

    def init_chat(self, chat_item):
        kwargs = {
            'dynamo': getattr(self, 'dynamo', None),
            'flag_dynamo': getattr(self, 'flag_dynamo', None),
            'member_dynamo': getattr(self, 'member_dynamo', None),
            'view_dynamo': getattr(self, 'view_dynamo', None),
            'block_manager': self.block_manager,
            'chat_message_manager': self.chat_message_manager,
            'user_manager': self.user_manager,
        }
        return Chat(chat_item, **kwargs) if chat_item else None

    def add_direct_chat(self, chat_id, created_by_user_id, with_user_id, now=None):
        now = now or pendulum.now('utc')

        # can't direct chat with ourselves
        if created_by_user_id == with_user_id:
            raise ChatException(f'User `{created_by_user_id}` cannot open direct chat with themselves')

        # can't chat if there's a blocking relationship, either direction
        if self.block_manager.is_blocked(created_by_user_id, with_user_id):
            raise ChatException(f'User `{created_by_user_id}` has blocked user `{with_user_id}`')
        if self.block_manager.is_blocked(with_user_id, created_by_user_id):
            raise ChatException(f'User `{created_by_user_id}` has been blocked by `{with_user_id}`')

        # can't add a chat if one already exists between the two users
        if self.get_direct_chat(created_by_user_id, with_user_id):
            raise ChatException(
                f'Chat already exists between user `{created_by_user_id}` and user `{with_user_id}`',
            )

        transacts = [
            self.dynamo.transact_add(
                chat_id, ChatType.DIRECT, created_by_user_id, with_user_id=with_user_id, now=now,
            ),
            self.member_dynamo.transact_add(chat_id, created_by_user_id, now=now),
            self.member_dynamo.transact_add(chat_id, with_user_id, now=now),
            self.user_manager.dynamo.transact_increment_chat_count(created_by_user_id),
            self.user_manager.dynamo.transact_increment_chat_count(with_user_id),
        ]
        transact_exceptions = [
            ChatException(f'Unable to add chat with id `{chat_id}`... id already used?'),
            ChatException(f'Unable to add user `{created_by_user_id}` to chat `{chat_id}`'),
            ChatException(f'Unable to add user `{with_user_id}` to chat `{chat_id}`'),
            ChatException(f'Unable to increment User.chatCount for user `{created_by_user_id}`'),
            ChatException(f'Unable to increment User.chatCount for user `{with_user_id}`'),
        ]
        self.dynamo.client.transact_write_items(transacts, transact_exceptions)

        return self.get_chat(chat_id, strongly_consistent=True)

    def add_group_chat(self, chat_id, created_by_user, name=None, now=None):
        now = now or pendulum.now('utc')

        # create the group chat with just caller in it
        transacts = [
            self.dynamo.transact_add(chat_id, ChatType.GROUP, created_by_user.id, name=name, now=now),
            self.member_dynamo.transact_add(chat_id, created_by_user.id, now=now),
            self.user_manager.dynamo.transact_increment_chat_count(created_by_user.id),
        ]
        transact_exceptions = [
            ChatException(f'Unable to add chat with id `{chat_id}`... id already used?'),
            ChatException(f'Unable to add user `{created_by_user.id}` to chat `{chat_id}`'),
            ChatException(f'Unable to increment User.chatCount for user `{created_by_user.id}`'),
        ]
        self.dynamo.client.transact_write_items(transacts, transact_exceptions)

        self.chat_message_manager.add_system_message_group_created(chat_id, created_by_user, name=name, now=now)
        return self.get_chat(chat_id, strongly_consistent=True)

    def leave_all_chats(self, user_id):
        user = None
        for chat_id in self.member_dynamo.generate_chat_ids_by_user(user_id):
            chat = self.get_chat(chat_id)
            if not chat:
                logger.warning(f'Unable to find chat `{chat_id}` that user `{user_id}` is member of, ignoring')
                continue
            if chat.type == ChatType.DIRECT:
                chat.delete_direct_chat()
            else:
                user = user or self.user_manager.get_user(user_id)
                chat.leave(user)

    def record_views(self, chat_ids, user_id, viewed_at=None):
        for chat_id, view_count in dict(collections.Counter(chat_ids)).items():
            chat = self.get_chat(chat_id)
            if not chat:
                logger.warning(f'Cannot record view(s) by user `{user_id}` on DNE chat `{chat_id}`')
            elif not chat.is_member(user_id):
                logger.warning(f'Cannot record view(s) by non-member user `{user_id}` on chat `{chat_id}`')
            else:
                chat.record_view_count(user_id, view_count, viewed_at=viewed_at)

    def on_chat_message_add(self, message_id, new_item):
        message = self.chat_message_manager.init_chat_message(new_item)
        self.dynamo.update_last_message_activity_at(message.chat_id, message.created_at, fail_soft=True)
        self.dynamo.increment_messages_count(message.chat_id)

        # for each memeber of the chat
        #   - update the last message activity timestamp (controls chat ordering)
        #   - for everyone except the author, increment their 'messagesUnviewedCount'
        for user_id in self.member_dynamo.generate_user_ids_by_chat(message.chat_id):
            self.member_dynamo.update_last_message_activity_at(
                message.chat_id, user_id, message.created_at, fail_soft=True
            )
            if user_id != message.user_id:
                # Note that dynamo has no support for batch updates.
                self.member_dynamo.increment_messages_unviewed_count(message.chat_id, user_id)
                # TODO
                # we can be in a state where the user manually dismissed a card, and this view does not
                # change the user's overall count of chats with unread messages, but should still create a card

    def on_chat_message_delete(self, message_id, old_item):
        message = self.chat_message_manager.init_chat_message(old_item)
        self.dynamo.decrement_messages_count(message.chat_id, fail_soft=True)

        # for each memeber of the chat other than the author
        #   - delete any view record that exists directly on the message
        #   - determine if the message had status 'unviewed', and if so, then decrement the unviewed message counter
        for user_id in self.member_dynamo.generate_user_ids_by_chat(message.chat_id):
            if user_id != message.user_id:
                chat_view_item = self.view_dynamo.get_view(message.chat_id, user_id)
                chat_last_viewed_at = pendulum.parse(chat_view_item['lastViewedAt']) if chat_view_item else None
                if not (chat_last_viewed_at and chat_last_viewed_at > message.created_at):
                    # Note that dynamo has no support for batch updates.
                    self.member_dynamo.decrement_messages_unviewed_count(message.chat_id, user_id, fail_soft=True)

    def sync_member_messages_unviewed_count(self, chat_id, new_item, old_item=None):
        if new_item.get('viewCount', 0) > (old_item or {}).get('viewCount', 0):
            user_id = new_item['sortKey'].split('/')[1]
            self.member_dynamo.clear_messages_unviewed_count(chat_id, user_id)

    def on_flag_add(self, chat_id, new_item):
        chat_item = self.dynamo.increment_flag_count(chat_id)
        chat = self.init_chat(chat_item)

        # force delete the chat_message?
        if chat.is_crowdsourced_forced_removal_criteria_met():
            logger.warning(f'Force deleting chat `{chat_id}` from flagging')
            chat.delete()
