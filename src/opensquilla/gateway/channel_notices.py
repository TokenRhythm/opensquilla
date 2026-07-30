"""Backward-compatible imports for shared channel system messages.

New channel code should import from :mod:`opensquilla.channels.system_messages`
so channel rendering does not depend on the Gateway package.
"""

from opensquilla.channels.system_messages import (
    ChannelSystemMessageKey as ChannelNoticeKey,
)
from opensquilla.channels.system_messages import (
    channel_message_locale as channel_notice_locale,
)
from opensquilla.channels.system_messages import (
    render_channel_message as render_channel_notice,
)

__all__ = ["ChannelNoticeKey", "channel_notice_locale", "render_channel_notice"]
