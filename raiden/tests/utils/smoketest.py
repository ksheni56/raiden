import json
import os
import random
import shutil
from contextlib import contextmanager
from http import HTTPStatus

import click
import requests
from eth_typing import HexStr
from eth_utils import remove_0x_prefix, to_canonical_address
from gevent import sleep
from web3 import HTTPProvider, Web3
from web3.contract import Contract
from web3.middleware import geth_poa_middleware

from raiden.accounts import AccountManager
from raiden.connection_manager import ConnectionManager
from raiden.constants import (
    EMPTY_ADDRESS,
    GENESIS_BLOCK_NUMBER,
    SECONDS_PER_DAY,
    UINT256_MAX,
    Environment,
    EthClient,
)
from raiden.network.proxies.proxy_manager import ProxyManager, ProxyManagerMetadata
from raiden.network.proxies.user_deposit import UserDeposit
from raiden.network.rpc.client import JSONRPCClient
from raiden.settings import DEFAULT_NUMBER_OF_BLOCK_CONFIRMATIONS
from raiden.tests.fixtures.constants import DEFAULT_BALANCE, DEFAULT_PASSPHRASE
from raiden.tests.utils.eth_node import (
    AccountDescription,
    EthNodeDescription,
    GenesisDescription,
    eth_node_to_datadir,
    geth_keystore,
    parity_keystore,
    run_private_blockchain,
)
from raiden.tests.utils.smartcontracts import deploy_token
from raiden.transfer import channel, views
from raiden.transfer.state import ChannelState
from raiden.ui.app import run_app
from raiden.utils.formatting import to_checksum_address
from raiden.utils.http import HTTPExecutor
from raiden.utils.keys import privatekey_to_address
from raiden.utils.typing import (
    TYPE_CHECKING,
    Address,
    AddressHex,
    Any,
    Balance,
    BlockNumber,
    Callable,
    ChainID,
    Dict,
    Iterable,
    Iterator,
    List,
    MonitoringServiceAddress,
    OneToNAddress,
    Port,
    PrivateKey,
    TokenAddress,
    TokenAmount,
    TokenNetworkRegistryAddress,
    Tuple,
)
from raiden.waiting import wait_for_block
from raiden_contracts.constants import (
    CONTRACT_HUMAN_STANDARD_TOKEN,
    CONTRACT_MONITORING_SERVICE,
    CONTRACT_ONE_TO_N,
    CONTRACT_SECRET_REGISTRY,
    CONTRACT_SERVICE_REGISTRY,
    CONTRACT_TOKEN_NETWORK_REGISTRY,
    CONTRACT_USER_DEPOSIT,
    NETWORKNAME_TO_ID,
    TEST_SETTLE_TIMEOUT_MAX,
    TEST_SETTLE_TIMEOUT_MIN,
)
from raiden_contracts.contract_manager import ContractManager, contracts_precompiled_path

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from raiden.tests.utils.transport import ParsedURL  # noqa: F401

# the smoketest will assert that a different endpoint got successfully registered
TEST_DEPOSIT_AMOUNT = TokenAmount(5)

TEST_PRIVKEY = PrivateKey(
    b"\xad\xd4\xd3\x10\xba\x04$hy\x1d\xd7\xbf\x7fn\xae\x85\xac"
    b"\xc4\xdd\x14?\xfa\x81\x0e\xf1\x80\x9aj\x11\xf2\xbcD"
)
TEST_ACCOUNT_ADDRESS = privatekey_to_address(TEST_PRIVKEY)


def ensure_executable(cmd):
    """look for the given command and make sure it can be executed"""
    if not shutil.which(cmd):
        raise ValueError(
            "Error: unable to locate %s binary.\n"
            "Make sure it is installed and added to the PATH variable." % cmd
        )


def deploy_smoketest_contracts(
    client: JSONRPCClient,
    chain_id: ChainID,
    contract_manager: ContractManager,
    token_address: AddressHex,
) -> Dict[str, Address]:
    if client.eth_node is EthClient.GETH:
        client.web3.geth.personal.unlockAccount(client.web3.eth.accounts[0], DEFAULT_PASSPHRASE)
    elif client.eth_node is EthClient.PARITY:
        client.web3.parity.personal.unlockAccount(client.web3.eth.accounts[0], DEFAULT_PASSPHRASE)

    contract_proxy, _ = client.deploy_single_contract(
        contract_name=CONTRACT_SECRET_REGISTRY,
        contract=contract_manager.get_contract(CONTRACT_SECRET_REGISTRY),
        constructor_parameters=None,
    )
    secret_registry_address = Address(to_canonical_address(contract_proxy.address))

    secret_registry_constructor_arguments = (
        to_checksum_address(secret_registry_address),
        chain_id,
        TEST_SETTLE_TIMEOUT_MIN,
        TEST_SETTLE_TIMEOUT_MAX,
        UINT256_MAX,
    )

    contract_proxy, _ = client.deploy_single_contract(
        contract_name=CONTRACT_TOKEN_NETWORK_REGISTRY,
        contract=contract_manager.get_contract(CONTRACT_TOKEN_NETWORK_REGISTRY),
        constructor_parameters=secret_registry_constructor_arguments,
    )
    token_network_registry_address = Address(to_canonical_address(contract_proxy.address))

    service_registry_constructor_arguments = (
        token_address,
        EMPTY_ADDRESS,
        int(500e18),
        6,
        5,
        180 * SECONDS_PER_DAY,
        1000,
        200 * SECONDS_PER_DAY,
    )
    service_registry_contract, _ = client.deploy_single_contract(
        contract_name=CONTRACT_SERVICE_REGISTRY,
        contract=contract_manager.get_contract(CONTRACT_SERVICE_REGISTRY),
        constructor_parameters=service_registry_constructor_arguments,
    )
    service_registry_address = Address(to_canonical_address(service_registry_contract.address))

    user_deposit_contract, _ = client.deploy_single_contract(
        contract_name=CONTRACT_USER_DEPOSIT,
        contract=contract_manager.get_contract(CONTRACT_USER_DEPOSIT),
        constructor_parameters=(token_address, UINT256_MAX),
    )
    user_deposit_address = Address(to_canonical_address(user_deposit_contract.address))

    monitoring_service_contract, _ = client.deploy_single_contract(
        contract_name=CONTRACT_MONITORING_SERVICE,
        contract=contract_manager.get_contract(CONTRACT_MONITORING_SERVICE),
        constructor_parameters=(
            token_address,
            service_registry_address,
            user_deposit_address,
            token_network_registry_address,
        ),
    )
    monitoring_service_address = Address(to_canonical_address(monitoring_service_contract.address))

    one_to_n_contract, _ = client.deploy_single_contract(
        contract_name=CONTRACT_ONE_TO_N,
        contract=contract_manager.get_contract(CONTRACT_ONE_TO_N),
        constructor_parameters=(user_deposit_address, chain_id, service_registry_address),
    )
    one_to_n_address = Address(to_canonical_address(one_to_n_contract.address))

    proxy_manager = ProxyManager(
        rpc_client=client,
        contract_manager=contract_manager,
        metadata=ProxyManagerMetadata(
            token_network_registry_deployed_at=GENESIS_BLOCK_NUMBER,
            filters_start_at=GENESIS_BLOCK_NUMBER,
        ),
    )
    user_deposit_proxy = UserDeposit(
        jsonrpc_client=client,
        user_deposit_address=user_deposit_contract.address,
        contract_manager=contract_manager,
        proxy_manager=proxy_manager,
        block_identifier="latest",
    )
    user_deposit_proxy.init(
        monitoring_service_address=MonitoringServiceAddress(monitoring_service_address),
        one_to_n_address=OneToNAddress(one_to_n_address),
        given_block_identifier="latest",
    )

    addresses = {
        CONTRACT_SECRET_REGISTRY: secret_registry_address,
        CONTRACT_TOKEN_NETWORK_REGISTRY: token_network_registry_address,
        CONTRACT_SERVICE_REGISTRY: service_registry_address,
        CONTRACT_USER_DEPOSIT: user_deposit_address,
        CONTRACT_MONITORING_SERVICE: monitoring_service_address,
        CONTRACT_ONE_TO_N: one_to_n_address,
    }

    return addresses


def get_private_key(keystore):
    accmgr = AccountManager(keystore)
    if not accmgr.accounts:
        raise RuntimeError("No Ethereum accounts found in the user's system")

    addresses = list(accmgr.accounts.keys())
    return accmgr.get_privkey(addresses[0], DEFAULT_PASSPHRASE)


@contextmanager
def setup_testchain(
    eth_client: EthClient, free_port_generator: Iterator[Port], base_datadir: str, base_logdir: str
) -> Iterator[Dict[str, Any]]:

    ensure_executable(eth_client.value)

    rpc_port = next(free_port_generator)
    p2p_port = next(free_port_generator)

    eth_rpc_endpoint = f"http://127.0.0.1:{rpc_port}"
    web3 = Web3(HTTPProvider(endpoint_uri=eth_rpc_endpoint))
    web3.middleware_onion.inject(geth_poa_middleware, layer=0)

    eth_nodes = [
        EthNodeDescription(
            private_key=TEST_PRIVKEY,
            rpc_port=rpc_port,
            p2p_port=p2p_port,
            miner=True,
            extra_config={},
            blockchain_type=eth_client.value,
        )
    ]

    random_marker = remove_0x_prefix(HexStr(hex(random.getrandbits(100))))
    genesis_description = GenesisDescription(
        prefunded_accounts=[AccountDescription(TEST_ACCOUNT_ADDRESS, DEFAULT_BALANCE)],
        random_marker=random_marker,
        chain_id=NETWORKNAME_TO_ID["smoketest"],
    )

    datadir = eth_node_to_datadir(privatekey_to_address(TEST_PRIVKEY), base_datadir)
    if eth_client is EthClient.GETH:
        keystore = geth_keystore(datadir)
    elif eth_client is EthClient.PARITY:
        keystore = parity_keystore(datadir)

    eth_node_runner = run_private_blockchain(
        web3=web3,
        eth_nodes=eth_nodes,
        base_datadir=base_datadir,
        log_dir=base_logdir,
        verbosity="info",
        genesis_description=genesis_description,
    )
    with eth_node_runner as node_executors:
        yield dict(
            eth_client=eth_client,
            base_datadir=base_datadir,
            eth_rpc_endpoint=eth_rpc_endpoint,
            keystore=keystore,
            node_executors=node_executors,
            web3=web3,
        )


@contextmanager
def setup_matrix_for_smoketest(
    print_step: Callable,
    free_port_generator: Iterable[Port],
    broadcast_rooms_aliases: Iterable[str],
) -> Iterator[List[Tuple["ParsedURL", HTTPExecutor]]]:
    from raiden.tests.utils.transport import matrix_server_starter

    print_step("Starting Matrix transport")

    with matrix_server_starter(free_port_generator, broadcast_rooms_aliases) as ctx:
        yield ctx


@contextmanager
def setup_testchain_for_smoketest(
    eth_client: EthClient,
    print_step: Callable,
    free_port_generator: Iterator[Port],
    base_datadir: str,
    base_logdir: str,
) -> Iterator[Dict[str, Any]]:
    print_step("Starting Ethereum node")

    with setup_testchain(
        eth_client=eth_client,
        free_port_generator=free_port_generator,
        base_datadir=base_datadir,
        base_logdir=base_logdir,
    ) as ctx:
        yield ctx


def setup_raiden(
    matrix_server,
    print_step,
    contracts_version,
    eth_client,
    eth_rpc_endpoint,
    web3,
    base_datadir,
    keystore,
) -> Dict[str, Any]:
    print_step("Deploying Raiden contracts")

    if eth_client is EthClient.PARITY:
        client = JSONRPCClient(
            web3, get_private_key(keystore), gas_estimate_correction=lambda gas: gas * 2
        )
    else:
        client = JSONRPCClient(web3, get_private_key(keystore))
    contract_manager = ContractManager(contracts_precompiled_path(contracts_version))

    proxy_manager = ProxyManager(
        rpc_client=client,
        contract_manager=contract_manager,
        metadata=ProxyManagerMetadata(
            token_network_registry_deployed_at=GENESIS_BLOCK_NUMBER,
            filters_start_at=GENESIS_BLOCK_NUMBER,
        ),
    )

    token = deploy_token(
        deploy_client=client,
        contract_manager=contract_manager,
        initial_amount=TokenAmount(1000),
        decimals=0,
        token_name="TKN",
        token_symbol="TKN",
        token_contract_name=CONTRACT_HUMAN_STANDARD_TOKEN,
    )
    contract_addresses = deploy_smoketest_contracts(
        client=client,
        chain_id=NETWORKNAME_TO_ID["smoketest"],
        contract_manager=contract_manager,
        token_address=to_checksum_address(token.address),
    )
    confirmed_block_identifier = client.get_confirmed_blockhash()
    registry = proxy_manager.token_network_registry(
        TokenNetworkRegistryAddress(contract_addresses[CONTRACT_TOKEN_NETWORK_REGISTRY]),
        block_identifier=confirmed_block_identifier,
    )

    registry.add_token(
        token_address=TokenAddress(to_canonical_address(token.address)),
        channel_participant_deposit_limit=TokenAmount(UINT256_MAX),
        token_network_deposit_limit=TokenAmount(UINT256_MAX),
        given_block_identifier=confirmed_block_identifier,
    )

    print_step("Setting up Raiden")
    user_deposit_contract_address = to_checksum_address(contract_addresses[CONTRACT_USER_DEPOSIT])

    args = {
        "address": to_checksum_address(TEST_ACCOUNT_ADDRESS),
        "datadir": keystore,
        "eth_rpc_endpoint": eth_rpc_endpoint,
        "gas_price": "fast",
        "keystore_path": keystore,
        "matrix_server": matrix_server,
        "network_id": str(NETWORKNAME_TO_ID["smoketest"]),
        "password_file": click.File()(os.path.join(base_datadir, "pw")),
        "user_deposit_contract_address": user_deposit_contract_address,
        "sync_check": False,
        "environment_type": Environment.DEVELOPMENT,
    }

    # Wait until the secret registry is confirmed, otherwise the App
    # inialization will fail, needed for the check
    # `check_ethereum_confirmed_block_is_not_pruned`.
    current_block = client.block_number()
    target_block_number = current_block + DEFAULT_NUMBER_OF_BLOCK_CONFIRMATIONS
    while current_block < target_block_number:
        current_block = client.block_number()
        sleep(0.5)

    return {"args": args, "contract_addresses": contract_addresses, "token": token}


def run_smoketest(
    print_step: Callable,
    args: Dict[str, Any],
    contract_addresses: Dict[str, Address],
    token: Contract,
) -> None:
    print_step("Starting Raiden")

    app = None
    try:
        app = run_app(**args)
        raiden_api = app.raiden.raiden_api
        assert raiden_api is not None  # for mypy

        block = BlockNumber(app.raiden.get_block_number() + DEFAULT_NUMBER_OF_BLOCK_CONFIRMATIONS)
        # Proxies now use the confirmed block hash to query the chain for
        # prerequisite checks. Wait a bit here to make sure that the confirmed
        # block hash contains the deployed token network or else things break
        wait_for_block(raiden=app.raiden, block_number=block, retry_timeout=1.0)

        raiden_api.channel_open(
            registry_address=TokenNetworkRegistryAddress(
                contract_addresses[CONTRACT_TOKEN_NETWORK_REGISTRY]
            ),
            token_address=TokenAddress(to_canonical_address(token.address)),
            partner_address=ConnectionManager.BOOTSTRAP_ADDR,
        )
        raiden_api.set_total_channel_deposit(
            registry_address=TokenNetworkRegistryAddress(
                contract_addresses[CONTRACT_TOKEN_NETWORK_REGISTRY]
            ),
            token_address=TokenAddress(to_canonical_address(token.address)),
            partner_address=ConnectionManager.BOOTSTRAP_ADDR,
            total_deposit=TEST_DEPOSIT_AMOUNT,
        )
        token_addresses = [to_checksum_address(token.address)]

        print_step("Running smoketest")

        raiden_service = app.raiden
        token_network_added_events = raiden_service.default_registry.filter_token_added_events()
        events_token_addresses = [
            event["args"]["token_address"] for event in token_network_added_events
        ]

        assert events_token_addresses == token_addresses

        token_networks = views.get_token_identifiers(
            views.state_from_raiden(raiden_service), raiden_service.default_registry.address
        )
        assert len(token_networks) == 1

        channel_state = views.get_channelstate_for(
            chain_state=views.state_from_raiden(raiden_service),
            token_network_registry_address=raiden_service.default_registry.address,
            token_address=token_networks[0],
            partner_address=ConnectionManager.BOOTSTRAP_ADDR,
        )
        assert channel_state

        distributable = channel.get_distributable(
            channel_state.our_state, channel_state.partner_state
        )
        assert distributable == TEST_DEPOSIT_AMOUNT
        assert Balance(distributable) == channel_state.our_state.contract_balance
        assert channel.get_status(channel_state) == ChannelState.STATE_OPENED

        port_number = raiden_service.config.rest_api.port
        response = requests.get(f"http://localhost:{port_number}/api/v1/channels")

        assert response.status_code == HTTPStatus.OK

        response_json = json.loads(response.content)
        assert response_json[0]["partner_address"] == to_checksum_address(
            ConnectionManager.BOOTSTRAP_ADDR
        )
        assert response_json[0]["state"] == "opened"
        assert int(response_json[0]["balance"]) > 0
    finally:
        if app is not None:
            app.stop()
            app.raiden.greenlet.get()
