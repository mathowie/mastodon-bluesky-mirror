"""Single-attempt official Threads container and publication API."""
from ..core.threads_auth import (
    ThreadsAPIError, ThreadsAuth, ThreadsAuthError, ThreadsTransportError,
    identifier, request_json,
)


class ThreadsClient:
    def __init__(self, config=None, session=None):
        self.auth = ThreadsAuth(config=config, session=session)

    @property
    def account_id(self):
        return self.auth.authorize().account_id

    def connection_status(self):
        return self.auth.connection_status()

    def maintain_credentials(self):
        """Renew verified credentials even when no source posts are eligible."""
        self.auth.authorize()

    def _request(self, method, path, *, params=None, data=None, credentials=None):
        credentials = credentials if credentials is not None else self.auth.authorize()
        return request_json(self.auth.session, method, path, token=credentials.access_token,
                            params=params, data=data)

    @staticmethod
    def _created_id(body):
        try:
            return identifier(body.get("id"))
        except ThreadsAPIError:
            # A successful write without its ID may already exist remotely.
            raise ThreadsTransportError() from None

    def create_container(self, payload: dict) -> str:
        allowed = {"media_type", "text", "image_url", "alt_text", "reply_to_id", "children", "is_carousel_item"}
        if (not isinstance(payload, dict) or set(payload) - allowed
                or payload.get("media_type") not in ("TEXT", "IMAGE", "CAROUSEL")):
            raise ThreadsAPIError("Invalid Threads container payload")
        data = dict(payload)
        if "reply_to_id" in data:
            data["reply_to_id"] = identifier(data["reply_to_id"])
        if "children" in data:
            children = data["children"]
            if isinstance(children, str):
                children = children.split(",")
            if not isinstance(children, (list, tuple)) or not children:
                raise ThreadsAPIError("Invalid Threads carousel children")
            data["children"] = ",".join(identifier(child) for child in children)
        if "is_carousel_item" in data:
            flag = data["is_carousel_item"]
            if type(flag) is bool:
                flag = "true" if flag else "false"
            if not isinstance(flag, str) or flag not in ("true", "false"):
                raise ThreadsAPIError("Invalid Threads carousel item flag")
            data["is_carousel_item"] = flag
        credentials = self.auth.authorize()
        body = self._request("POST", f"/{credentials.account_id}/threads", data=data, credentials=credentials)
        return self._created_id(body)

    def container_status(self, container_id) -> dict:
        container_id = identifier(container_id)
        body = self._request("GET", f"/{container_id}", params={"fields": "id,status,error_message"})
        if (body.get("id") != container_id
                or body.get("status") not in ("FINISHED", "IN_PROGRESS", "PUBLISHED", "ERROR", "EXPIRED")):
            raise ThreadsAPIError("Threads returned invalid container metadata")
        result = {"id": container_id, "status": body["status"]}
        if body.get("error_message"):
            result["error_message"] = "Threads could not process the container"
        return result

    def publish_container(self, container_id) -> str:
        container_id = identifier(container_id)
        credentials = self.auth.authorize()
        body = self._request("POST", f"/{credentials.account_id}/threads_publish",
                             data={"creation_id": container_id}, credentials=credentials)
        return self._created_id(body)

    def media_details(self, media_id) -> dict:
        media_id = identifier(media_id)
        body = self._request("GET", f"/{media_id}", params={"fields": "id,permalink,text"})
        if body.get("id") != media_id:
            raise ThreadsAPIError("Threads returned invalid media metadata")
        return {key: body[key] for key in ("id", "permalink", "text") if key in body}
