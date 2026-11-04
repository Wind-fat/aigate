from aigate.security import generate_api_key, hash_api_key, is_well_formed


def test_generate_api_key_shape():
    raw, prefix, digest = generate_api_key()
    assert raw.startswith("sk-ag-")
    assert prefix == raw[:14]
    assert len(digest) == 64
    assert digest == hash_api_key(raw)


def test_hash_is_deterministic_and_not_reversible():
    raw = "sk-ag-abcdefghijklmnop"
    assert hash_api_key(raw) == hash_api_key(raw)
    assert hash_api_key(raw) != raw
    # different input -> different digest
    assert hash_api_key(raw + "x") != hash_api_key(raw)


def test_keys_are_unique():
    keys = {generate_api_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_is_well_formed():
    assert is_well_formed("sk-ag-" + "a" * 32)
    assert not is_well_formed("sk-xx-" + "a" * 32)
    assert not is_well_formed("sk-ag-short")
