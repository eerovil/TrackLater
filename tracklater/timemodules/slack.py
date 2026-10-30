from time import sleep
import asyncio

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk import WebClient
from datetime import datetime
import pytz
from typing import List

from tracklater import settings
from .interfaces import EntryMixin, AbstractParser, AbstractProvider
from tracklater.models import Entry

import logging
logger = logging.getLogger(__name__)


class Parser(EntryMixin, AbstractParser):

    async def async_parse_channel(self, provider, channel, user_id, users, group):
        logger.warning("Getting channel %s for group %s", channel['id'], group)
        try:
            history = await provider.api_call(
                "conversations.history",
                data={
                    'channel': channel['id'],
                    'oldest': (self.start_date - datetime(1970, 1, 1)).total_seconds(),
                    'latest': (self.end_date - datetime(1970, 1, 1)).total_seconds()
                }
            )
            # Get either Istant Message recipient or channel name
            if channel.get('is_im', False) and channel.get('user', ''):
                channel_info = users.get(channel.get('user', ''), None)
            else:
                channel_info = channel.get('name_normalized', channel['id'])

            for message in history['messages']:
                if message.get('user', '') == user_id:
                    start_time = datetime.fromtimestamp(float(message['ts']))
                    # "Guess" That the timestamp has an offset equal to settings.TIMEZONE
                    # if getattr(settings, 'TIMEZONE', None):
                    #     start_time = pytz.timezone("Europe/Helsinki").localize(
                    #         start_time
                    #     ).astimezone(pytz.utc).replace(tzinfo=None)
                    # Replace @User id with the name
                    for _user_id in users.keys():
                        if _user_id in message['text']:
                            message['text'] = message['text'].replace(
                                _user_id, users[_user_id]
                            )
                    logger.warning("Found message %s", "{} - {} \n {}".format(group, channel_info, message['text']))
                    self.async_entries.append(Entry(
                        start_time=start_time,
                        title='',
                        text="{} - {} \n {}".format(group, channel_info, message['text']),
                        group=group
                    ))
        except Exception as e:
            logger.warning("Error %s", e)

    def get_entries(self) -> List[Entry]:
        # start_date, end_date = self.get_offset_dates()
        # if not start_date and not end_date:
        #     return []
        start_date = self.start_date
        end_date = self.end_date
        for group, group_data in settings.SLACK.items():
            slack_token = group_data['API_KEY']
            user_id = group_data['USER_ID']

            provider = Provider(slack_token)
            users_list = provider.api_call("users.list")
            users = {}
            for user in users_list['members']:
                users[user['id']] = (
                    user['profile'].get('first_name', 'NULL') + ' ' +
                    user['profile'].get('last_name', 'NULL')
                )
            im_channels = provider.api_call(
                "conversations.list", data={'types': 'mpim,im,private_channel', 'limit': 1000}
            )['channels']
            channels = im_channels + [{"id": channel_id} for channel_id in group_data.get('CHANNELS', [])]
            self.async_entries: List[Entry] = []
            async_provider = AsyncProvider(slack_token)
            async def get_channels(channels):
                await asyncio.gather(*[
                    self.async_parse_channel(async_provider, channel, user_id, users, group)
                    for channel in channels
                ])

            asyncio.run(get_channels(channels))
                
        return self.async_entries


class AsyncProvider(AbstractProvider):
    def __init__(self, slack_token):
        self.sc = AsyncWebClient(slack_token)

    async def api_call(self, *args, **kwargs):
        try:
            return await self.sc.api_call(*args, **kwargs)
        except Exception:
            sleep(1)
            return await self.sc.api_call(*args, **kwargs)


class Provider(AbstractProvider):
    def __init__(self, slack_token):
        self.sc = WebClient(slack_token)

    def api_call(self, *args, **kwargs):
        try:
            return self.sc.api_call(*args, **kwargs)
        except Exception:
            sleep(1)
            return self.sc.api_call(*args, **kwargs)

    def test_api_call(self, *args, **kwargs):
        if args[0] == "users.list":
            return {
                "members": [
                    {
                        "id": "1",
                        "profile": {
                            "first_name": "Firstname",
                            "last_name": "Lastename"
                        }
                    },
                    {
                        "id": "2",
                        "profile": {
                            "first_name": "Secondname",
                            "last_name": "Lastename"
                        }
                    }
                ]
            }
        if args[0] == "conversations.list":
            return {"channels": [{"id": "1"}]}
        if args[0] == "conversations.history":
            return {
                "messages": [
                    {
                        "user": "1",
                        "text": "First Message",
                        "ts": "1234567"
                    },
                    {
                        "user": "1",
                        "text": "Second Message",
                        "ts": "1234568"
                    },
                    {
                        "user": "2",
                        "text": "Third Message",
                        "ts": "1234569"
                    }
                ]
            }
