from agent.agent import Agent
from channel.base import Channel
from config import Settings


def create_channel(channel_type: str, agent: Agent, settings: Settings) -> Channel:
    if channel_type == "cli":
        from channel.cli.cli_channel import CLIChannel
        return CLIChannel(agent, settings)
    if channel_type == "feishu":
        from channel.feishu.feishu_channel import FeishuChannel
        return FeishuChannel(agent, settings)
    raise ValueError(f"unknown channel type: {channel_type}")
