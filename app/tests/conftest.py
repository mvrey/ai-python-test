import pytest
import respx

from service.config import Settings
from service.provider_http import create_provider_client

PROVIDER_URL = "http://provider.test"
API_KEY = "secret"


@pytest.fixture
async def provider_http():
    """The real provider HTTP client, pointed at a fake base URL."""
    client = create_provider_client(Settings(provider_base_url=PROVIDER_URL, provider_api_key=API_KEY))
    yield client
    await client.aclose()


@pytest.fixture
def provider():
    """respx router intercepting calls to the fake provider."""
    with respx.mock(base_url=PROVIDER_URL, assert_all_called=False) as mock:
        yield mock
