"""Focused tests for the LMS->IELTS membership consumer (SSO_SYNC_DESIGN.md §10).

Run: python manage.py test core.test_lms_sync
Uses APIRequestFactory (no Firebase/network). Self-contained env gating via override_settings-
style env vars set per test.
"""

import os
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from core.lms_sync_views import LmsMembershipSyncView
from core.models import User


def _post(body, api_key="secret", **env):
    factory = APIRequestFactory()
    headers = {}
    if api_key is not None:
        headers["HTTP_X_API_KEY"] = api_key
    req = factory.post("/api/lms/students/membership", body, format="json", **headers)
    base = {"LMS_SYNC_API_KEY": "secret", "SYNC_CONSUME_MEMBERSHIP": "true"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        return LmsMembershipSyncView.as_view()(req)


def _member(email="stu@x.io", program="ielts", event="member.upserted", group="G-1"):
    return {"event_type": event, "program_type": program, "group_name": group,
            "student": {"email": email, "name": "Stu"}}


class LmsMembershipConsumerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(uid="u1", role="student", email="stu@x.io", first_name="Stu", group=None)

    def test_disabled_returns_503(self):
        res = _post(_member(), SYNC_CONSUME_MEMBERSHIP="false")
        self.assertEqual(res.status_code, 503)

    def test_bad_key_401(self):
        res = _post(_member(), api_key="wrong")
        self.assertEqual(res.status_code, 401)

    def test_missing_key_config_503(self):
        res = _post(_member(), api_key="x", LMS_SYNC_API_KEY="")
        self.assertEqual(res.status_code, 503)

    def test_non_ielts_program_skipped(self):
        res = _post(_member(program="sat"))
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.group)          # not touched

    def test_user_not_found_404(self):
        res = _post(_member(email="ghost@x.io"))
        self.assertEqual(res.status_code, 404)

    def test_upserted_sets_group_label(self):
        res = _post(_member(group="IELTS-Morning"))
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.group, "IELTS-Morning")

    def test_match_is_case_insensitive(self):
        res = _post(_member(email="STU@X.io", group="G-2"))
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.group, "G-2")

    def test_removed_clears_only_matching_group(self):
        self.user.group = "G-1"; self.user.save()
        res = _post(_member(event="member.removed", group="G-1"))
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.group)

    def test_removed_is_noop_for_different_group(self):
        # move safety: removing OLD must not clear a label already pointing at NEW
        self.user.group = "NEW"; self.user.save()
        _post(_member(event="member.removed", group="OLD"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.group, "NEW")
