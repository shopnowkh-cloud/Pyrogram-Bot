#!/usr/bin/env python3
import os
import requests
from typing import Optional

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

def restore_session(mail_address: str, restore_key: str) -> Optional[dict]:
    data = _gql('mutation { introduceSession(input: { withAddress: false }) { id } }')
    new_id = (data.get("data", {}).get("introduceSession") or {}).get("id")
    if not new_id:
        return None
    r = _gql("""
    mutation Restore($mailAddress: String!, $restoreKey: String!, $sessionId: ID!) {
        restoreAddress(input: { mailAddress: $mailAddress, restoreKey: $restoreKey, sessionId: $sessionId }) {
            id address restoreKey
        }
    }
    """, {"mailAddress": mail_address, "restoreKey": restore_key, "sessionId": new_id})
    addr = r.get("data", {}).get("restoreAddress")
    if not addr:
        return None
    return {"session_id": new_id, "email": addr.get("address"),
            "address_id": addr.get("id"), "restore_key": addr.get("restoreKey")}

def delete_address(address_id: str) -> bool:
    try:
        data = _gql('mutation Delete($a: ID!) { deleteAddress(input: { addressId: $a }) }',
                    {"a": address_id})
        return bool(data.get("data", {}).get("deleteAddress"))
    except Exception:
        return False

def get_new_mails(session_id: str, after_mail_id: Optional[str] = None):
    if after_mail_id:
        query = """
        query GetMails($id: ID!, $mailId: ID!) {
            session(id: $id) {
                mailsAfterId(mailId: $mailId) { id fromAddr toAddr headerSubject text }
            }
        }
        """
        variables = {"id": session_id, "mailId": after_mail_id}
    else:
        query = """
        query GetMails($id: ID!) {
            session(id: $id) {
                mails { id fromAddr toAddr headerSubject text }
            }
        }
        """
        variables = {"id": session_id}
    data = _gql(query, variables)
    session_data = data.get("data", {}).get("session")
    if session_data is None:
        return None
    return session_data.get("mailsAfterId") or session_data.get("mails") or []
