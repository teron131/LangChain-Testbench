import asyncio
from typing import List, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .interview import interview_graph
from .models import Editor, Outline, WikiSection
from .outline import get_initial_outline, refine_outline_chain, survey_subjects
from .writer import initialize_vectorstore, section_writer, writer


class ResearchState(TypedDict):
    topic: str
    outline: Outline
    editors: List[Editor]
    interview_results: List[dict]  # List of InterviewState
    sections: List[WikiSection]
    article: str


async def initialize_research(state: ResearchState):
    topic = state["topic"]
    coros = (
        get_initial_outline(topic),
        survey_subjects.ainvoke(topic),
    )
    results = await asyncio.gather(*coros)
    return {
        **state,
        "outline": results[0],
        "editors": results[1].editors,
    }


async def conduct_interviews(state: ResearchState):
    topic = state["topic"]
    initial_states = [
        {
            "editor": editor,
            "messages": [
                {
                    "type": "ai",
                    "content": f"So you said you were writing an article on {topic}?",
                    "name": "expert_bot",
                }
            ],
        }
        for editor in state["editors"]
    ]
    # We call in to the sub-graph here to parallelize the interviews
    interview_results = await interview_graph.abatch(initial_states)

    return {
        **state,
        "interview_results": interview_results,
    }


def format_conversation(interview_state):
    messages = interview_state["messages"]
    convo = "\n".join(f"{m.name}: {m.content}" for m in messages)
    return f'Conversation with {interview_state["editor"].name}\n\n' + convo


async def refine_outline(state: ResearchState):
    convos = "\n\n".join([format_conversation(interview_state) for interview_state in state["interview_results"]])

    updated_outline = await refine_outline_chain.ainvoke(
        {
            "topic": state["topic"],
            "old_outline": state["outline"].as_str,
            "conversations": convos,
        }
    )
    return {**state, "outline": updated_outline}


async def index_references(state: ResearchState):
    all_docs = {}
    for interview_state in state["interview_results"]:
        all_docs.update(interview_state["references"])
    await initialize_vectorstore(all_docs)
    return state


async def write_sections(state: ResearchState):
    outline = state["outline"]
    sections = await section_writer.abatch(
        [
            {
                "outline": outline.as_str,
                "section": section.section_title,
                "topic": state["topic"],
            }
            for section in outline.sections
        ]
    )
    return {
        **state,
        "sections": sections,
    }


async def write_article(state: ResearchState):
    topic = state["topic"]
    sections = state["sections"]
    draft = "\n\n".join([section.as_str for section in sections])
    article = await writer.ainvoke({"topic": topic, "draft": draft})
    return {
        **state,
        "article": article,
    }


# Create the graph
builder = StateGraph(ResearchState)

nodes = [
    ("init_research", initialize_research),
    ("conduct_interviews", conduct_interviews),
    ("refine_outline", refine_outline),
    ("index_references", index_references),
    ("write_sections", write_sections),
    ("write_article", write_article),
]

for i in range(len(nodes)):
    name, node = nodes[i]
    builder.add_node(name, node)
    if i > 0:
        builder.add_edge(nodes[i - 1][0], name)

builder.add_edge(START, nodes[0][0])
builder.add_edge(nodes[-1][0], END)

storm = builder.compile(checkpointer=MemorySaver())


def generate_article(topic: str) -> str:
    """Generate a Wikipedia-style article on a given topic using the STORM pipeline.

    Args:
        topic (str): The topic to generate an article about

    Returns:
        str: The generated Wikipedia-style article
    """
    config = {"configurable": {"thread_id": "my-thread"}}

    async def _generate():
        async for _ in storm.astream({"topic": topic}, config):
            pass
        checkpoint = storm.get_state(config)
        return checkpoint.values["article"]

    return asyncio.run(_generate())
