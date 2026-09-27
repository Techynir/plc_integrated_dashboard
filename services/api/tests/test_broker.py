from app.broker import Hub, Subscriber


def test_hub_filters_by_device():
    hub = Hub()
    everyone, only_a = Subscriber(), Subscriber(devices={"a"})
    hub.add(everyone)
    hub.add(only_a)
    hub.handle_app_message("app/live/a", b'{"type":"live","device_id":"a"}')
    hub.handle_app_message("app/live/b", b'{"type":"live","device_id":"b"}')
    hub.handle_app_message("app/alarms", b'{"type":"alarm"}')
    assert everyone.queue.qsize() == 3
    assert only_a.queue.qsize() == 2


def test_hub_caches_stats():
    hub = Hub()
    hub.handle_app_message("app/stats", b'{"type":"stats","received_total":5}')
    assert hub.ingestor_stats["received_total"] == 5
