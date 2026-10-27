from omm.cluster.member_auth import (NonceCache, head_proof, leaf_sha256, member_key, sign_request, verify_head,
                                     verify_request)

NS_A = "omm-" + "a" * 32
NS_B = "omm-" + "b" * 32


def test_member_key_is_not_the_namespace():
    assert member_key(NS_A) != NS_A.encode() and len(member_key(NS_A)) == 32
    assert member_key(NS_A) == member_key(NS_A) != member_key(NS_B)


def test_signed_request_verifies_once():
    key, cache = member_key(NS_A), NonceCache()
    headers = sign_request(key, "POST", "/omm/member/poll", "12D3node", now=1000, nonce="n1")
    assert verify_request(key, "POST", "/omm/member/poll", headers, now=1010, nonces=cache) == "12D3node"
    assert verify_request(key, "POST", "/omm/member/poll", headers, now=1010, nonces=cache) is None


def test_request_outside_window_or_other_path_fails():
    key = member_key(NS_A)
    headers = sign_request(key, "POST", "/omm/member/poll", "n", now=1000, nonce="x")
    assert verify_request(key, "POST", "/omm/member/poll", headers, now=1121, nonces=NonceCache()) is None
    assert verify_request(key, "POST", "/omm/member/poll", headers, now=879, nonces=NonceCache()) is None
    assert verify_request(key, "GET", "/omm/member/rag/snapshot", headers, now=1000, nonces=NonceCache()) is None


def test_other_cluster_key_fails():
    headers = sign_request(member_key(NS_B), "POST", "/p", "n", now=1, nonce="x")
    assert verify_request(member_key(NS_A), "POST", "/p", headers, now=1, nonces=NonceCache()) is None


def test_missing_or_garbled_headers_fail():
    key = member_key(NS_A)
    headers = sign_request(key, "POST", "/p", "n", now=1, nonce="x")
    for name in list(headers):
        partial = {k: v for k, v in headers.items() if k != name}
        assert verify_request(key, "POST", "/p", partial, now=1, nonces=NonceCache()) is None
    assert verify_request(key, "POST", "/p", {**headers, "X-Omm-Time": "soon"}, now=1, nonces=NonceCache()) is None


def test_nonce_cache_forgets_after_window():
    cache = NonceCache()
    assert cache.seen("n", 1000) is False
    assert cache.seen("n", 1001) is True
    assert cache.seen("n", 1000 + 2 * 121) is False


def test_head_proof_rejects_other_certificate():
    key = member_key(NS_A)
    proof = head_proof(key, "n1", "aa" * 32, "bb" * 32)
    assert verify_head(key, "n1", "aa" * 32, "bb" * 32, proof)
    assert not verify_head(key, "n1", "cc" * 32, "bb" * 32, proof)
    assert not verify_head(key, "n1", "aa" * 32, "dd" * 32, proof)
    assert not verify_head(key, "n2", "aa" * 32, "bb" * 32, proof)
    assert not verify_head(member_key(NS_B), "n1", "aa" * 32, "bb" * 32, proof)
    assert not verify_head(key, "n1", "aa" * 32, "bb" * 32, None)


def test_leaf_sha256_is_lowercase_hex():
    assert leaf_sha256(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
