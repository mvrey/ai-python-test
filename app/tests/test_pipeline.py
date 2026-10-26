from service.config import Settings
from service.pipeline import Pipeline


def test_submit_accepts_until_queue_is_full():
    pipeline = Pipeline(Settings(queue_maxsize=2))

    assert pipeline.submit("a")
    assert pipeline.submit("b")
    assert not pipeline.submit("c")
