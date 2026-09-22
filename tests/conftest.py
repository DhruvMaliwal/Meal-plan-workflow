import datetime as dt
import pytest

from mealplan.attributes import AttributeStore
from mealplan.history import build_history
from mealplan.inventory import normalise_extraction
from mealplan.llm import LLM
from mealplan.profiles import load_profile
from mealplan.repo import get_repo


@pytest.fixture(scope="session")
def repo():
    return get_repo()


@pytest.fixture(scope="session")
def store(repo, tmp_path_factory):
    s = AttributeStore(repo, path=tmp_path_factory.mktemp("cache") / "attrs.json")
    s.ensure_all()
    return s


@pytest.fixture(scope="session")
def profile():
    return load_profile("gurupriyan_raksha")


@pytest.fixture(scope="session")
def llm():
    return LLM(mock=True)


@pytest.fixture(scope="session")
def inventory(repo, llm):
    return normalise_extraction(llm.extract_inventory([]), repo, source="sample")


@pytest.fixture(scope="session")
def history(repo, llm):
    return build_history(llm.extract_last_plan([]), repo)


@pytest.fixture(scope="session")
def start_date():
    return dt.date(2026, 9, 25)
