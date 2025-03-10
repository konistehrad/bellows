import asyncio
import contextlib
import functools
import logging

import click
import zigpy.config as zigpy_conf

import bellows.ezsp
import bellows.types as t

LOGGER = logging.getLogger(__name__)


class CSVParamType(click.ParamType):
    name = "comma separated integers"

    def __init__(self, min=None, max=None):
        self.intrange = click.IntRange(min, max)

    def convert(self, value, param, ctx):
        values = [self.intrange.convert(v, param, ctx) for v in value.split(",")]
        return values


class ZigbeeNodeParamType(click.ParamType):
    name = "colon separated hex bytes"

    def convert(self, value, param, ctx):
        if ":" not in value or len(value) != 23:
            self.fail("Node format should be a 8 byte hex string separated by ':'")
        return t.EmberEUI64.convert(value)


def background(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(f(*args, **kwargs))

    return inner


def app(f, app_startup=True, extra_config=None):
    database_file = None
    application = None

    async def async_inner(ctx, *args, **kwargs):
        nonlocal database_file
        nonlocal application
        app_config = {
            zigpy_conf.CONF_DEVICE: {
                zigpy_conf.CONF_DEVICE_PATH: ctx.obj["device"],
                zigpy_conf.CONF_DEVICE_BAUDRATE: ctx.obj["baudrate"],
                zigpy_conf.CONF_FLOW_CONTROL: ctx.obj["flow_control"],
            },
            zigpy_conf.CONF_DATABASE: ctx.obj["database_file"],
        }
        if extra_config:
            app_config.update(extra_config)
        application = await setup_application(app_config, startup=app_startup)
        ctx.obj["app"] = application
        await f(ctx, *args, **kwargs)
        await asyncio.sleep(0.5)
        await application.shutdown()

    def shutdown():
        with contextlib.suppress(Exception):
            application._ezsp.close()

    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(async_inner(*args, **kwargs))
        except:  # noqa: E722
            # It seems that often errors like a message send will try to send
            # two messages, and not reading all of them will leave the NCP in
            # a bad state. This seems to mitigate this somewhat. Better way?
            loop.run_until_complete(asyncio.sleep(0.5))
            raise
        finally:
            shutdown()

    return inner


def print_cb(frame_name, response):
    click.echo(f"Callback: {frame_name} {response}")


def channel_mask(channels):
    mask = 0
    for channel in channels:
        if not (11 <= channel <= 26):
            raise click.BadOptionUsage("channels must be from 11 to 26")
        mask |= 1 << channel
    return mask


async def setup(dev, baudrate, cbh=None, configure=True):
    app_config = bellows.zigbee.application.ControllerApplication.SCHEMA(
        {
            zigpy_conf.CONF_DEVICE: {
                zigpy_conf.CONF_DEVICE_PATH: dev,
                zigpy_conf.CONF_DEVICE_BAUDRATE: baudrate,
                zigpy_conf.CONF_DEVICE_FLOW_CONTROL: zigpy_conf.CONF_DEVICE_FLOW_CONTROL_DEFAULT,
            }
        }
    )

    app = bellows.zigbee.application.ControllerApplication(app_config)
    await app.connect()

    if cbh:
        app._ezsp.add_callback(cbh)

    return app._ezsp


async def setup_application(app_config, startup=True):
    app_config = bellows.zigbee.application.ControllerApplication.SCHEMA(app_config)
    app = await bellows.zigbee.application.ControllerApplication.new(
        app_config, start_radio=startup
    )
    return app


def check(ret, message, expected=0):
    if ret == expected:
        return
    if isinstance(expected, list) and ret in expected:
        return
    raise click.ClickException(message)


async def network_init(s):
    v = await s.networkInit()
    check(
        v[0],
        f"Failure initializing network: {v[0]}",
        [0, t.EmberStatus.NOT_JOINED],
    )
    return v


def parse_epan(epan):
    """Parse a user specified extended PAN ID"""
    epan_list = [t.uint8_t(x, 16) for x in epan.split(":")]
    return t.fixed_list(8, t.uint8_t)(epan_list)


async def basic_tc_permits(s):
    async def set_policy(policy, decision):
        v = await s.setPolicy(policy, decision)
        check(v[0], f"Failed to set policy {policy} to {decision}: {v[0]}")

    await set_policy(
        s.types.EzspPolicyId.TC_KEY_REQUEST_POLICY,
        s.types.EzspDecisionId.DENY_TC_KEY_REQUESTS,
    )
    await set_policy(
        s.types.EzspPolicyId.APP_KEY_REQUEST_POLICY,
        s.types.EzspDecisionId.ALLOW_APP_KEY_REQUESTS,
    )
    await set_policy(
        s.types.EzspPolicyId.TRUST_CENTER_POLICY,
        s.types.EzspDecisionId.ALLOW_PRECONFIGURED_KEY_JOINS,
    )


def get_device(app, node):
    if node not in app.devices:
        click.echo(f"Device {node} is not in the device database")
        return None

    return app.devices[node]


def get_endpoint(app, node, endpoint_id):
    dev = get_device(app, node)
    if dev is None:
        return (dev, None)

    if endpoint_id not in dev.endpoints:
        click.echo("Device %s has no endpoint %d" % (node, endpoint_id))
        return (dev, None)

    return (dev, dev.endpoints[endpoint_id])


def get_in_cluster(app, node, endpoint_id, cluster_id):
    dev, endpoint = get_endpoint(app, node, endpoint_id)
    if endpoint is None:
        return (dev, endpoint, None)

    if cluster_id not in endpoint.in_clusters:
        click.echo(
            "Device %s has no cluster %d on endpoint %d"
            % (node, cluster_id, endpoint_id)
        )
        return (dev, endpoint, None)

    return (dev, endpoint, endpoint.in_clusters[cluster_id])
