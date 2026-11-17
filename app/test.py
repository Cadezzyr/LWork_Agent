from langchain.agents import create_agent
from langchain_deepseek import ChatDeepSeek
from app.core.config import PROJECT_ROOT, Settings, get_settings

settings: Settings = get_settings()

def get_weather(city: str) -> str:
    """Get weather for a given city."""
    return f"It's always sunny in {city}!"

# 在这里传入 api_key、base_url
llm = ChatDeepSeek(
    model="deepseek-chat",
    api_key=settings.api_key,
    # base_url=settings.DEEPSEEK_BASE_URL, # 私有化/代理时加上
)

agent = create_agent(
    model=llm,   # 传实例，不再传字符串"deepseek"
    tools=[get_weather],
)

for chunk in agent.stream(
    {"messages": [{"role": "user", "content": "What is the weather in SF?"}]},
    stream_mode="updates",
    version="v2",
):
    print(chunk)
    # if chunk["type"] == "updates":
    #     for step, data in chunk["data"].items():
    #         print(f"step: {step}")
    #         print(f"content: {data['messages'][-1].content_blocks}")
