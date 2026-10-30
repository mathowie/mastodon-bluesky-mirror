"""Read the authenticated user's Mastodon account timeline."""
import logging

from mastodon import Mastodon

from ..config import settings

logger = logging.getLogger(__name__)


class MastodonClient:
    def __init__(self):
        self.api = Mastodon(
            access_token=settings.MASTODON_ACCESS_TOKEN,
            api_base_url=settings.MASTODON_API_BASE_URL,
            request_timeout=15,
            ratelimit_method="throw",
        )

    def get_recent_posts(self, limit=20):
        # account(id) looks up a specific account. It cannot identify the user.
        logger.info("Verifying Mastodon credentials")
        me = self.api.account_verify_credentials()
        logger.info("Fetching the authenticated Mastodon account's recent posts")
        posts = self.api.account_statuses(me["id"], limit=limit)
        logger.info("Fetched %d Mastodon statuses", len(posts))
        return posts

    def get_status(self, status_id):
        return self.api.status(status_id)
