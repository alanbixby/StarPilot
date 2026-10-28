import http.client
import json
import threading

import pytest

from openpilot.starpilot.galaxy.access import GalaxyAccessOwner
from openpilot.starpilot.galaxy.server import make_server
from openpilot.starpilot.gps.phone import PhoneGpsPublisher, phone_gps_status
from openpilot.starpilot.navigation.owner import NavigationOwner

NOW = 101_000_000_000
FIX = {'latitude': 40., 'longitude': -110., 'horizontalAccuracy': 5., 'ageMs': 300, 'speed': 20.}
PLACE = {'name': 'Library', 'latitude': 40., 'longitude': -110.}


@pytest.fixture
def numeric_http(tmp_path):
  access = GalaxyAccessOwner(tmp_path / 'access')
  assert access.configure('password123', lambda: True)
  navigation = NavigationOwner(tmp_path / 'navigation', runtime_source=lambda: None, transient_root=tmp_path / 'boot-navigation')
  path = tmp_path / 'phone.json'
  now = [NOW]
  phone = PhoneGpsPublisher(path, lambda: now[0])
  server = make_server(port=0, owner=access, navigation=navigation, phone_location=phone, parked=lambda: True)
  worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
  worker.start()

  def request(route, payload, cookie=''):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
      connection.request('POST', route, json.dumps(payload), {
        'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{server.server_port}', 'Cookie': cookie,
      })
      response = connection.getresponse()
      return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
      connection.close()

  try:
    assert request('/api/companion/location', FIX)[0] == 401
    status, _, headers = request('/api/auth/login', {'password': 'password123'})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';', 1)[0]
    yield lambda route, payload: request(route, payload, cookie)[:2], navigation, path, now
  finally:
    server.shutdown()
    worker.join(timeout=2)
    server.server_close()


@pytest.mark.parametrize('field', ('latitude', 'longitude', 'horizontalAccuracy', 'ageMs', 'speed'))
@pytest.mark.parametrize('value', [10 ** 400, -(10 ** 400)], ids=['huge', 'negative-huge'])
def test_huge_phone_number_returns_400_and_preserves_usable_fix(numeric_http, field, value):
  request, _, path, now = numeric_http
  assert request('/api/companion/location', FIX) == (200, {'ok': True})
  previous = path.read_bytes()
  now[0] += 1_000_000_000
  assert request('/api/companion/location', {**FIX, field: value}) == (400, {'error': f'Invalid {field}'})
  assert path.read_bytes() == previous
  assert phone_gps_status(now[0], path) == {
    'reason': f'Invalid {field}', 'accepted': False, 'usable': True, 'receivedAgeMs': 0., 'fixAgeMs': 1300.,
  }
  assert request('/api/companion/location', FIX) == (200, {'ok': True})


@pytest.mark.parametrize('action', ['select', 'favorite'])
@pytest.mark.parametrize('field', ['latitude', 'longitude'])
@pytest.mark.parametrize('value', [10 ** 400, -(10 ** 400)], ids=['huge', 'negative-huge'])
def test_huge_destination_number_returns_400_without_changing_saved_places(numeric_http, action, field, value):
  request, navigation, _, _ = numeric_http
  for operation in ('select', 'favorite'):
    assert request('/api/navigation/action', {
      'action': operation, 'revision': navigation.read()['revision'], 'destination': PLACE,
    })[0] == 200
  before = navigation.read()
  assert request('/api/navigation/action', {
    'action': action, 'revision': before['revision'], 'destination': {**PLACE, field: value},
  }) == (400, {'error': 'Destination name and coordinates are invalid'})
  assert navigation.read() == before
  assert request('/api/navigation/action', {
    'action': action, 'revision': before['revision'], 'destination': PLACE,
  })[0] == 200
