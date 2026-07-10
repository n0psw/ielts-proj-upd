"""Cross-platform sync — IELTS membership consumer (see SSO_SYNC_DESIGN.md §10).

IELTS has no group entity; a student's group is a denormalized ``User.group`` label. So the LMS
pushes only student membership here (member.upserted / member.removed, IELTS-program only) over
the same api/lms + X-API-Key path SAT uses. We set/clear the matched user's ``group`` label.

Self-contained auth + flag gating (mirrors the SAT ApiKeyAuth contract): 503 when unconfigured or
disabled, 401 on a bad key — so it is a strict no-op until ``LMS_SYNC_API_KEY`` and
``SYNC_CONSUME_MEMBERSHIP`` are both set. No model/migration change (matches by unique email).
"""

import hmac
import os

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import User

_TRUTHY = {"1", "true", "yes", "on"}


def _sync_enabled() -> bool:
    return os.getenv("SYNC_CONSUME_MEMBERSHIP", "").strip().lower() in _TRUTHY


class LmsMembershipSyncView(APIView):
    # Auth is the X-API-Key check below (service-to-service), not Firebase.
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        expected = (os.getenv("LMS_SYNC_API_KEY", "") or "").strip()
        provided = (request.META.get("HTTP_X_API_KEY", "") or "").strip()
        # Contract shared with the CRM/LMS: 503 when the server has no key, 401 on mismatch.
        if not expected:
            return Response({"error": "LMS sync not configured (LMS_SYNC_API_KEY)"}, status=503)
        if not provided or not hmac.compare_digest(provided, expected):
            return Response({"error": "Invalid API Key"}, status=401)
        if not _sync_enabled():
            return Response({"error": "Membership sync disabled (SYNC_CONSUME_MEMBERSHIP)"}, status=503)

        data = request.data or {}
        student = data.get("student") or {}
        email = (student.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "student.email is required"}, status=400)

        program = (data.get("program_type") or "").strip().lower()
        # IELTS only owns IELTS-program memberships; others are accepted-and-skipped so the LMS
        # drainer marks them delivered rather than retrying.
        if program != "ielts":
            return Response({"status": "skipped", "reason": f"non-IELTS program '{program}'"}, status=200)

        evt = (data.get("event_type") or "member.upserted").strip().lower()
        group_name = (data.get("group_name") or "")[:128]

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            # Not provisioned on IELTS yet — retryable; the LMS drainer dead-letters to an operator
            # if it never arrives, rather than dropping it.
            return Response({"error": "user not found", "email": email}, status=404)

        if evt == "member.removed":
            # Conditional clear: only if the label still points at this group (so a move =
            # remove(old)+add(new) is order-independent and can't clobber the new assignment).
            if (user.group or "") == group_name:
                user.group = None
                user.save(update_fields=["group"])
                return Response({"status": "unassigned"}, status=200)
            return Response({"status": "noop"}, status=200)

        # member.upserted
        user.group = group_name
        user.save(update_fields=["group"])
        return Response({"status": "assigned", "group": user.group}, status=200)
