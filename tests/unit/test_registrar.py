from digitalplat_auto_register.core.registrar import DigitalPlatRegistrar
from digitalplat_auto_register.types import DigitalPlatConfig


def test_prepare_user_profile_generates_required_defaults():
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())

    profile = registrar._prepare_user_profile(
        username=None,
        email=None,
        fullname=None,
        phone=None,
        password=None,
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postal_code=None,
        country=None,
    )

    assert profile.username
    assert profile.password
    assert profile.fullname.count(" ") == 1
    assert profile.phone.startswith("+1-")
    assert profile.address_line1
    assert profile.city
    assert profile.state
    assert profile.postal_code
    assert profile.country == "US"


def test_prepare_user_profile_preserves_supplied_values():
    registrar = DigitalPlatRegistrar(DigitalPlatConfig())

    profile = registrar._prepare_user_profile(
        username="provided-user",
        email="provided@example.com",
        fullname="Provided User",
        phone="+1-555-555-5555",
        password="ProvidedPass1!",
        address_line1="1 Provided Street",
        address_line2="Suite 2",
        city="Austin",
        state="TX",
        postal_code="78701",
        country="US",
    )

    assert profile.username == "provided-user"
    assert profile.fullname == "Provided User"
    assert profile.phone == "+1-555-555-5555"
    assert profile.address_line1 == "1 Provided Street"
    assert profile.address_line2 == "Suite 2"
    assert profile.city == "Austin"
    assert profile.state == "TX"
    assert profile.postal_code == "78701"
