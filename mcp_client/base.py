"""
This module contains the base functions and classes for the MCP client.
"""

import json
import os
from typing import List, Type

from langchain.tools.base import BaseTool, ToolException
from langchain_core.prompts import ChatPromptTemplate
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain.chat_models import init_chat_model
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import BaseModel
from jsonschema_pydantic import jsonschema_to_pydantic

CONFIG_FILE = 'mcp-server-config.json'


def create_mcp_tool(
        tool_schema: types.Tool,
        server_params: StdioServerParameters
) -> BaseTool:
    """Create a LangChain tool from MCP tool schema.

    This function generates a new LangChain tool based on the provided MCP tool schema
    and server parameters. The tool's behavior is defined within the McpTool inner class.

    :param tool_schema: The schema of the tool to be created.
    :param server_params: The server parameters needed by the tool for operation.
    :return: An instance of a newly created mcp tool.
    """

    # Convert the input schema to a Pydantic model for validation
    input_model = jsonschema_to_pydantic(tool_schema.inputSchema)

    class McpTool(BaseTool):
        """McpTool class represents a tool that can execute operations asynchronously."""

        # Tool attributes from the schema
        name: str = tool_schema.name
        description: str = tool_schema.description
        args_schema: Type[BaseModel] = input_model
        mcp_server_params: StdioServerParameters = server_params

        def _run(self, **kwargs):
            """Synchronous execution is not supported."""
            raise NotImplementedError("Only async operations are supported")

        async def _arun(self, **kwargs):
            """Run the tool asynchronously with provided arguments."""
            async with stdio_client(self.mcp_server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()  # Initialize the session
                    result = await session.call_tool(self.name, arguments=kwargs)
                    if result.isError:
                        # Raise an exception if there is an error in the tool call
                        raise ToolException(result.content)
                    return result.content  # Return the result if no error

    return McpTool()


async def convert_mcp_to_langchain_tools(server_params: List[StdioServerParameters]) -> List[BaseTool]:
    """Convert MCP tools to LangChain tools."""
    langchain_tools = []

    for server_param in server_params:
        tools = await get_mcp_tools(server_param)
        langchain_tools.extend(tools)

    return langchain_tools


async def get_mcp_tools(server_param: StdioServerParameters) -> List[BaseTool]:
    """Asynchronously retrieves and converts tools from a server using specified parameters"""
    mcp_tools = []

    async with stdio_client(server_param) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()  # Initialize the session
            tools: types.ListToolsResult = await session.list_tools()  # Retrieve tools from the server
            # Convert each tool to LangChain format and add to list
            for tool in tools.tools:
                mcp_tools.append(create_mcp_tool(tool, server_param))

    return mcp_tools


def load_server_config() -> dict:
    """Load server configuration from available config files."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)  # Load server configuration
    raise FileNotFoundError(f"Could not find config file {CONFIG_FILE}")


def create_server_parameters(server_config: dict) -> List[StdioServerParameters]:
    """Create server parameters from the server configuration."""
    server_parameters = []
    for config in server_config["mcpServers"].values():
        server_parameter = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env={**config.get("env", {}), "PATH": os.getenv("PATH")}
        )
        for key, value in server_parameter.env.items():
            if len(value) == 0 and key in os.environ:
                server_parameter.env[key] = os.getenv(key)
        server_parameters.append(server_parameter)
    return server_parameters


def initialize_model(llm_config: dict):
    """Initialize the language model using the provided configuration."""
    api_key = llm_config.get("api_key")
    init_args = {
        "model": llm_config.get("model", "gpt-4o-mini"),
        "model_provider": llm_config.get("provider", "openai"),
        "temperature": llm_config.get("temperature", 0),
        "streaming": True,
    }
    if api_key:
        init_args["api_key"] = api_key
    return init_chat_model(**init_args)


def create_chat_prompt(client: str, server_config: dict) -> ChatPromptTemplate:
    """Create chat prompt template from server configuration."""
    system_prompt = server_config.get("systemPrompt", "")
    if client == "rest":
        system_prompt = system_prompt + "\nGive the output in the json format only. Please do not include json formatting. Give plain json output."
    return ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("user", "{messages}"),
        ("placeholder", "{agent_scratchpad}"),
    ])


async def initialise_tools(client: str) -> AgentExecutor:
    """Initializes tools for the server."""
    server_config = load_server_config()
    server_params = create_server_parameters(server_config)
    langchain_tools = await convert_mcp_to_langchain_tools(server_params)

    model = initialize_model(server_config.get("llm", {}))
    prompt = create_chat_prompt(client, server_config)

    agent = create_tool_calling_agent(model, langchain_tools, prompt)

    agent_executor = AgentExecutor(agent=agent, tools=langchain_tools)

    return agent_executor
