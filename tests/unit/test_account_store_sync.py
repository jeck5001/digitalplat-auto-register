import asyncio

from digitalplat_auto_register.core.account import Account, AccountStatus, AccountStore
from digitalplat_auto_register.core.account_pool import AccountPool


def build_account(status=AccountStatus.PENDING):
    return Account(
        id="legacy-account-1",
        username="legacy-user",
        email="legacy@example.test",
        password="secret",
        status=status,
    )


def test_account_store_crud_keeps_account_pool_in_sync(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    pool = AccountPool(str(tmp_path / "pool.db"))
    store.set_pool(pool)
    account = store.create_account(build_account())

    synced = pool.get_account(account.id)
    assert synced is not None
    assert synced.profile["email"] == account.email
    assert synced.metadata["legacy_status"] == "pending"

    updated = store.update_account(account.id, status=AccountStatus.ACTIVE, email="new@example.test")
    assert updated is not None
    synced = pool.get_account(account.id)
    assert synced is not None
    assert synced.status == "active"
    assert synced.profile["email"] == "new@example.test"
    assert synced.metadata["legacy_status"] == "active"

    pool.record_usage(account.id, success=True, domain="example.test")
    store.update_account(account.id, fullname="Updated User")
    synced = pool.get_account(account.id)
    assert synced is not None
    assert synced.metrics.total_uses == 1
    assert synced.domain_registered == ["example.test"]

    assert store.delete_account(account.id) is True
    assert pool.get_account(account.id) is None


def test_account_store_load_reconciles_pool(tmp_path):
    accounts_path = tmp_path / "accounts.json"
    first_store = AccountStore(accounts_path)
    first_store.create_account(build_account(AccountStatus.ACTIVE))
    asyncio.run(first_store.save())

    pool = AccountPool(str(tmp_path / "pool.db"))
    second_store = AccountStore(accounts_path)
    second_store.set_pool(pool)
    asyncio.run(second_store.load())

    synced = pool.get_account("legacy-account-1")
    assert synced is not None
    assert synced.status == "active"
    assert len(pool.list_all_accounts()) == 1


def test_account_store_load_removes_stale_legacy_pool_rows(tmp_path):
    pool = AccountPool(str(tmp_path / "pool.db"))
    stale = build_account()
    pool.sync_account(
        profile=AccountStore()._account_to_profile(stale),
        account_id=stale.id,
    )

    store = AccountStore(tmp_path / "accounts.json")
    store.set_pool(pool)
    asyncio.run(store.load())

    assert pool.get_account(stale.id) is None


def test_setting_pool_after_load_reconciles_existing_accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create_account(build_account(AccountStatus.ACTIVE))
    asyncio.run(store.save())
    asyncio.run(store.load())

    pool = AccountPool(str(tmp_path / "pool.db"))
    store.set_pool(pool)

    assert pool.get_account("legacy-account-1") is not None
