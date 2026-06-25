from langgraph.graph import StateGraph, MessagesState, END


def hello_node(state: MessagesState):
    return {"messages": [{"role": "assistant", "content": "hello from langgraph dev"}]}


builder = StateGraph(MessagesState)
builder.add_node("hello", hello_node)
builder.set_entry_point("hello")
builder.add_edge("hello", END)
graph = builder.compile()
