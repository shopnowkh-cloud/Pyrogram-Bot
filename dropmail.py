#!/usr/bin/env python3
import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

def _get_url():
    token = os.environ.get("DROPMAIL_API_TOKEN", "")
    return f"https://dropmail.me/api/graphql/{token}"

def _gql(query: str, variables: Optional[dict] = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(_get_url(), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()

def create_session() -> Optional[dict]:
    """Start a new session and create a random email address."""
    query = """
    mutation {
        introduceSession {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """
    data = _gql(query)
    session = data.get("data", {}).get("introduceSession")
    if not session:
        return None
    addr = session["addresses"][0] if session.get("addresses") else {}
    return {
        "session_id": session["id"],
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def check_session(session_id: str) -> Optional[dict]:
    """
    Verify if a session is still alive.
    Returns session info dict if alive, None if expired/not found.
    """
    data = _gql("""
    query Check($id: ID!) {
        session(id: $id) {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """, {"id": session_id})
    session = (data.get("data") or {}).get("session")
    if not session:
        return None
    addr = session["addresses"][0] if session.get("addresses") else {}
    return {
        "session_id":  session["id"],
        "expires_at":  session.get("expiresAt"),
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def find_session_by_address(email_address: str) -> Optional[dict]:
    """
    Search all active sessions for one containing the given email address.
    Useful for recovery after bot restart when session_id might be stale.
    Returns session info dict if found, None otherwise.
    """
    data = _gql("""
    {
        sessions {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """)
    sessions = (data.get("data") or {}).get("sessions") or []
    for session in sessions:
        for addr in session.get("addresses") or []:
            if addr.get("address") == email_address:
                return {
                    "session_id":  session["id"],
                    "expires_at":  session.get("expiresAt"),
                    "email":       addr["address"],
                    "address_id":  addr.get("id"),
                    "restore_key": addr.get("restoreKey"),
                }
    return None

def restore_session(mail_address: str, restore_key: str) -> Optional[dict]:
    """
    Restore an INACTIVE address into a new blank session.
    Returns {"already_in_use": True} if the address is still active in another session.
    Returns None if the restore key is wrong or address is expired beyond recovery.
    """
    data = _gql('mutation { introduceSession(input: { withAddress: false }) { id } }')
    new_id = (data.get("data", {}).get("introduceSession") or {}).get("id")
    if not new_id:
        logger.warning('restore_session: could not create blank session')
        return None
    r = _gql("""
    mutation Restore($mailAddress: String!, $restoreKey: String!, $sessionId: ID!) {
        restoreAddress(input: { mailAddress: $mailAddress, restoreKey: $restoreKey, sessionId: $sessionId }) {
            id address restoreKey
        }
    }
    """, {"mailAddress": mail_address, "restoreKey": restore_key, "sessionId": new_id})

    errors = r.get("errors") or []
    for e in errors:
        msg = e.get("message", "")
        code = (e.get("extensions") or {}).get("code", "")
        logger.warning(f'restore_session error: code={code} msg={msg}')
        if msg == "already_in_use" or code == "already_in_use":
            return {"already_in_use": True}

    addr = r.get("data", {}).get("restoreAddress")
    if not addr:
        return None
    return {
        "session_id":  new_id,
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def delete_address(address_id: str) -> bool:
    """Remove address from session. Mail history is preserved. Address can be restored later."""
    try:
        data = _gql('mutation Delete($a: ID!) { deleteAddress(input: { addressId: $a }) }',
                    {"a": address_id})
        return bool(data.get("data", {}).get("deleteAddress"))
    except Exception:
        return False

def get_new_mails(session_id: str, after_mail_id: Optional[str] = None):
    """
    Fetch mails for a session.
    Returns None if session is expired/not found.
    Returns [] if session is alive but no new mails.
    Returns list of mail dicts if there are new mails.
    """
    if after_mail_id:
        query = """
        query GetMails($id: ID!, $mailId: ID!) {
            session(id: $id) {
                mailsAfterId(mailId: $mailId) { id fromAddr toAddr headerSubject text receivedAt }
            }
        }
        """
        variables = {"id": session_id, "mailId": after_mail_id}
    else:
        query = """
        query GetMails($id: ID!) {
            session(id: $id) {
                mails { id fromAddr toAddr headerSubject text receivedAt }
            }
        }
        """
        variables = {"id": session_id}
    data = _gql(query, variables)
    session_data = data.get("data", {}).get("session")
    if session_data is None:
        return None  # session expired
    return session_data.get("mailsAfterId") or session_data.get("mails") or []
