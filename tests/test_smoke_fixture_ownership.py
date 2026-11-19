from unittest.mock import Mock

import pytest
from requests.exceptions import ReadTimeout

from tests.integration import test_desktop_smoke as smoke


@pytest.mark.parametrize("lost_response", [False, True])
def test_desktop_fixture_reclaims_only_its_own_container_even_if_creation_response_is_lost(
    monkeypatch, lost_response
):
    client = Mock()
    container = Mock(attrs={"State": {"Health": {"Status": "healthy"}}})
    client.containers.run.side_effect = (
        ReadTimeout("creation response lost") if lost_response else None
    )
    client.containers.run.return_value = container
    client.containers.list.return_value = [container]
    monkeypatch.setattr(smoke.docker, "from_env", Mock(return_value=client))
    fixture = smoke.desktop.__wrapped__(None)
    if lost_response:
        with pytest.raises(ReadTimeout, match="creation response lost"):
            next(fixture)
    else:
        assert next(fixture) is container
        fixture.close()
    owner = client.containers.run.call_args.kwargs["labels"]["ai-desktop-agent.smoke"]
    if lost_response:
        client.containers.list.assert_called_once_with(
            all=True, filters={"label": "ai-desktop-agent.smoke=" + owner}
        )
    else:
        client.containers.list.assert_not_called()
    container.remove.assert_called_once_with(force=True)
    client.close.assert_called_once()
