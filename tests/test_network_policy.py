from pmc.sandbox import ContainerSandbox, GuardedSandbox, RestrictedUserSandbox


def test_network_capabilities_are_explicit_and_not_overclaimed():
    assert GuardedSandbox().supports_network_policy("full")
    assert not GuardedSandbox().supports_network_policy("none")
    assert not RestrictedUserSandbox().supports_network_policy("none")
    assert ContainerSandbox().supports_network_policy("none")
    assert not ContainerSandbox().supports_network_policy("restricted")
