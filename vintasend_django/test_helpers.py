from django.test import TestCase
from typing import TYPE_CHECKING

from model_bakery import baker

from django.contrib.auth import get_user_model

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser as DjangoUser


User = get_user_model()


class VintaSendDjangoTestCase(TestCase):
    def setUp(self):
        self._user_password = "123456"
        self.user: DjangoUser = baker.prepare(User, email="user@example.com")
        self.user.set_password(self._user_password)
        self.user.save()

    def create_user(self, **kwargs):
        return baker.make(User, **kwargs)
