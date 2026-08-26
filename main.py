import requests
import json
import math

from database import (
    initialize_database,

    create_user,
    get_user,
    list_users,

    create_conversation,
    list_conversations,
    conversation_belongs_to_user,

    save_message,
    load_messages,
    load_message_records,

    get_conversation_summary,
    update_conversation_summary,

    save_memory,
    load_memories,
    get_memory,
    update_memory,
    archive_memory,
    restore_memory,
    delete_memory,
    mark_memories_accessed,

    load_memories_without_embeddings,
    set_memory_embedding,
    clear_memory_embeddings
)


# =========================================================
# MODELS
# =========================================================

FAST_MODEL = "qwen3:4b"

DEFAULT_MODEL = "qwen3:8b"

DEEP_MODEL = "deepseek-r1:14b"

ROUTER_MODEL = FAST_MODEL

MEMORY_MODEL = FAST_MODEL

SESSION_SUMMARY_MODEL = DEFAULT_MODEL

EMBEDDING_MODEL = "nomic-embed-text"


# =========================================================
# MODEL ROUTING
# =========================================================

# Options:
# fast
# default
# deep
# auto

MODEL_MODE = "auto"

SHOW_ROUTER_ACTIVITY = True


# =========================================================
# OLLAMA
# =========================================================

OLLAMA_CHAT_URL = (
    "http://localhost:11434/api/chat"
)

OLLAMA_EMBED_URL = (
    "http://localhost:11434/api/embed"
)

OLLAMA_OLD_EMBED_URL = (
    "http://localhost:11434/api/embeddings"
)


# =========================================================
# MEMORY CONFIG
# =========================================================

AUTO_MEMORY = True

SHOW_MEMORY_ACTIVITY = True

MEMORY_RETRIEVAL_LIMIT = 8

MEMORY_MANAGER_LIMIT = 15

AUTO_LIFECYCLE_MIN_CONFIDENCE = 0.90


# =========================================================
# SESSION SUMMARY CONFIG
# =========================================================

SUMMARY_TRIGGER_MESSAGES = 18

RECENT_MESSAGE_LIMIT = 12

SUMMARY_BATCH_LIMIT = 24

MAX_SUMMARY_PASSES = 5

SHOW_SUMMARY_ACTIVITY = True


# =========================================================
# HELPERS
# =========================================================

def clamp_importance(value):

    try:
        value = int(value)

    except (
        ValueError,
        TypeError
    ):
        return 5

    return max(
        1,
        min(10, value)
    )


def clamp_confidence(value):

    try:
        value = float(value)

    except (
        ValueError,
        TypeError
    ):
        return 0.7

    return max(
        0.0,
        min(1.0, value)
    )


def memory_to_dict(memory):

    return {
        "id": memory[0],
        "content": memory[1],
        "category": memory[2],
        "importance": memory[3],
        "confidence": memory[4],
        "source": memory[5],
        "status": memory[6],
        "created_at": memory[7],
        "updated_at": memory[8],
        "last_accessed_at": memory[9],
        "access_count": memory[10],
        "embedding": memory[11],
        "merged_into_id": memory[12]
    }


# =========================================================
# MODEL ROUTER
# =========================================================

def model_name_from_mode(mode):

    if mode == "fast":
        return FAST_MODEL

    if mode == "deep":
        return DEEP_MODEL

    return DEFAULT_MODEL


def heuristic_route(message):

    text = message.lower()

    deep_keywords = [
        "debug",
        "architecture",
        "algorithm",
        "leetcode",
        "optimize",
        "complex",
        "reason",
        "reasoning",
        "prove",
        "math",
        "analyze deeply",
        "deep analysis",
        "step by step",
        "database design",
        "system design",
        "security",
        "performance issue"
    ]

    fast_keywords = [
        "short answer",
        "quick question",
        "briefly",
        "simple question",
        "translate",
        "rewrite",
        "grammar",
        "what does",
        "define"
    ]

    if any(
        keyword in text
        for keyword in deep_keywords
    ):
        return "deep"

    if any(
        keyword in text
        for keyword in fast_keywords
    ):
        return "fast"

    if len(message) < 80:
        return "fast"

    return "default"


def route_model(
    user_message
):

    if MODEL_MODE != "auto":

        return (
            MODEL_MODE,
            model_name_from_mode(
                MODEL_MODE
            )
        )

    router_prompt = """
You are a model router for a private AI assistant.

Choose which model tier should answer the user's message.

You have exactly three choices:

fast
default
deep

FAST:
Use for:
- simple questions
- casual conversation
- definitions
- straightforward factual explanations
- short rewriting
- grammar
- translation
- simple commands
- easy coding questions
- lightweight summaries

DEFAULT:
Use for:
- normal conversation
- most technical questions
- normal coding
- comparisons
- planning
- moderate reasoning
- explanations requiring some nuance
- most general-purpose requests

DEEP:
Use only when useful for:
- difficult debugging
- complex coding
- algorithms
- LeetCode-style reasoning
- math
- multi-step logical reasoning
- architecture decisions
- difficult planning
- subtle tradeoffs
- complex system design
- difficult technical analysis

Do NOT select deep merely because the message is long.

Prefer the cheapest model that can answer well.

Return ONLY valid JSON:

{
    "mode": "fast"
}

or:

{
    "mode": "default"
}

or:

{
    "mode": "deep"
}

Do not explain.
"""

    payload = {
        "model": ROUTER_MODEL,

        "messages": [
            {
                "role": "system",
                "content": router_prompt
            },
            {
                "role": "user",
                "content": user_message
            }
        ],

        "stream": False,

        "format": "json",

        "options": {
            "temperature": 0
        }
    }

    try:

        response = requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            timeout=120
        )

        response.raise_for_status()

        data = response.json()

        content = (
            data[
                "message"
            ][
                "content"
            ].strip()
        )

        result = json.loads(
            content
        )

        selected_mode = (
            str(
                result.get(
                    "mode",
                    "default"
                )
            )
            .lower()
            .strip()
        )

        if selected_mode not in [
            "fast",
            "default",
            "deep"
        ]:

            selected_mode = (
                heuristic_route(
                    user_message
                )
            )

    except Exception:

        selected_mode = (
            heuristic_route(
                user_message
            )
        )

    selected_model = (
        model_name_from_mode(
            selected_mode
        )
    )

    return (
        selected_mode,
        selected_model
    )


# =========================================================
# EMBEDDINGS
# =========================================================

def get_embedding(
    text,
    show_error=True
):

    text = text.strip()

    if not text:
        return None

    try:

        payload = {
            "model": EMBEDDING_MODEL,
            "input": text
        }

        response = requests.post(
            OLLAMA_EMBED_URL,
            json=payload,
            timeout=300
        )

        response.raise_for_status()

        data = response.json()

        embeddings = data.get(
            "embeddings"
        )

        if (
            embeddings
            and isinstance(
                embeddings,
                list
            )
        ):
            return embeddings[0]

    except Exception:
        pass

    try:

        payload = {
            "model": EMBEDDING_MODEL,
            "prompt": text
        }

        response = requests.post(
            OLLAMA_OLD_EMBED_URL,
            json=payload,
            timeout=300
        )

        response.raise_for_status()

        data = response.json()

        embedding = data.get(
            "embedding"
        )

        if embedding:
            return embedding

    except Exception as error:

        if show_error:

            print(
                "\nEmbedding error:"
                f" {error}\n"
            )

    return None


def parse_embedding(
    stored_embedding
):

    if stored_embedding is None:
        return None

    if isinstance(
        stored_embedding,
        list
    ):
        return stored_embedding

    try:

        return json.loads(
            stored_embedding
        )

    except (
        json.JSONDecodeError,
        TypeError
    ):
        return None


def cosine_similarity(
    vector_a,
    vector_b
):

    if (
        not vector_a
        or not vector_b
    ):
        return 0.0

    if (
        len(vector_a)
        != len(vector_b)
    ):
        return 0.0

    dot_product = sum(
        a * b

        for a, b in zip(
            vector_a,
            vector_b
        )
    )

    magnitude_a = math.sqrt(
        sum(
            a * a
            for a in vector_a
        )
    )

    magnitude_b = math.sqrt(
        sum(
            b * b
            for b in vector_b
        )
    )

    if (
        magnitude_a == 0
        or magnitude_b == 0
    ):
        return 0.0

    return (
        dot_product
        /
        (
            magnitude_a
            *
            magnitude_b
        )
    )


# =========================================================
# EMBEDDING INDEX
# =========================================================

def ensure_memory_embeddings():

    missing = (
        load_memories_without_embeddings()
    )

    if not missing:
        return

    print(
        f"\nIndexing "
        f"{len(missing)} memories..."
    )

    completed = 0

    for memory_id, content in missing:

        embedding = get_embedding(
            content
        )

        if embedding:

            set_memory_embedding(
                memory_id,
                embedding
            )

            completed += 1

    print(
        f"Memory index ready: "
        f"{completed}/"
        f"{len(missing)} indexed.\n"
    )


def rebuild_memory_embeddings():

    print(
        "\nRebuilding memory index..."
    )

    clear_memory_embeddings()

    ensure_memory_embeddings()


# =========================================================
# SEMANTIC MEMORY RETRIEVAL
# =========================================================

def retrieve_relevant_memories(
    user_id,
    query,
    limit=MEMORY_RETRIEVAL_LIMIT
):

    memories = load_memories(
        user_id
    )

    if not memories:
        return []

    query_embedding = (
        get_embedding(
            query,
            show_error=False
        )
    )

    if not query_embedding:

        results = []

        for memory in memories[
            :limit
        ]:

            item = memory_to_dict(
                memory
            )

            item[
                "semantic_score"
            ] = 0

            item[
                "ranking_score"
            ] = 0

            results.append(
                item
            )

        return results

    scored = []

    for memory in memories:

        item = memory_to_dict(
            memory
        )

        memory_embedding = (
            parse_embedding(
                item[
                    "embedding"
                ]
            )
        )

        if not memory_embedding:
            continue

        similarity = (
            cosine_similarity(
                query_embedding,
                memory_embedding
            )
        )

        importance_bonus = (
            item[
                "importance"
            ]
            / 10
        ) * 0.04

        confidence_bonus = (
            item[
                "confidence"
            ]
        ) * 0.03

        ranking_score = (
            similarity
            + importance_bonus
            + confidence_bonus
        )

        item[
            "semantic_score"
        ] = similarity

        item[
            "ranking_score"
        ] = ranking_score

        scored.append(
            item
        )

    scored.sort(
        key=lambda item:
            item[
                "ranking_score"
            ],

        reverse=True
    )

    return scored[
        :limit
    ]


# =========================================================
# SESSION SUMMARIZATION
# =========================================================

def create_updated_session_summary(
    existing_summary,
    message_records
):

    if not message_records:
        return existing_summary

    transcript_parts = []

    for message in message_records:

        role = (
            message[
                "role"
            ].upper()
        )

        transcript_parts.append(
            f"[Message {message['id']}] "
            f"{role}:\n"
            f"{message['content']}"
        )

    transcript = "\n\n".join(
        transcript_parts
    )

    prompt = """
You are the conversation-context summarizer for a private personal AI assistant.

Maintain a concise but useful summary of the ongoing conversation.

Preserve:

- current topic
- goals
- decisions made
- technical architecture
- important facts
- relevant preferences
- corrections
- unresolved questions
- project progress
- context required to continue naturally

Remove:

- greetings
- filler
- repetition
- obsolete information
- unnecessary verbosity

If later information corrects earlier information,
preserve the newer version.

Return ONLY the updated summary.
Do not include reasoning.
"""

    current = (
        existing_summary
        if existing_summary
        else "None yet."
    )

    user_content = (
        "CURRENT SESSION SUMMARY:\n"
        f"{current}\n\n"

        "NEW MESSAGES TO INCORPORATE:\n"
        f"{transcript}"
    )

    payload = {
        "model":
            SESSION_SUMMARY_MODEL,

        "messages": [
            {
                "role":
                    "system",

                "content":
                    prompt
            },
            {
                "role":
                    "user",

                "content":
                    user_content
            }
        ],

        "stream":
            False,

        "options": {
            "temperature": 0.2
        }
    }

    try:

        response = requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            timeout=300
        )

        response.raise_for_status()

        data = response.json()

        summary = (
            data[
                "message"
            ][
                "content"
            ].strip()
        )

        if summary:
            return summary

    except Exception as error:

        if SHOW_SUMMARY_ACTIVITY:

            print(
                "\nSession summary error:"
                f" {error}\n"
            )

    return None


def maybe_update_session_summary(
    user_id,
    conversation_id
):

    summary_info = (
        get_conversation_summary(
            conversation_id,
            user_id
        )
    )

    current_summary = (
        summary_info[
            "summary"
        ]
    )

    summarized_through = (
        summary_info[
            "summarized_through_message_id"
        ]
    )

    passes = 0

    while passes < MAX_SUMMARY_PASSES:

        unsummarized = (
            load_message_records(
                conversation_id,
                user_id,
                after_id=
                    summarized_through
            )
        )

        if (
            len(unsummarized)
            <= SUMMARY_TRIGGER_MESSAGES
        ):
            break

        candidates = (
            unsummarized[
                :-RECENT_MESSAGE_LIMIT
            ]
        )

        if not candidates:
            break

        batch = (
            candidates[
                :SUMMARY_BATCH_LIMIT
            ]
        )

        if not batch:
            break

        if SHOW_SUMMARY_ACTIVITY:

            print(
                "Context: summarizing "
                "older messages...",
                end="",
                flush=True
            )

        new_summary = (
            create_updated_session_summary(
                current_summary,
                batch
            )
        )

        if SHOW_SUMMARY_ACTIVITY:

            print(
                "\r"
                + " " * 80
                + "\r",
                end="",
                flush=True
            )

        if not new_summary:
            break

        summarized_through = (
            batch[-1][
                "id"
            ]
        )

        update_conversation_summary(
            conversation_id,
            user_id,
            new_summary,
            summarized_through
        )

        current_summary = (
            new_summary
        )

        passes += 1

        if SHOW_SUMMARY_ACTIVITY:

            print(
                "Context: session summary "
                f"updated through message "
                f"{summarized_through}.\n"
            )


def build_session_context(
    user_id,
    conversation_id
):

    summary_info = (
        get_conversation_summary(
            conversation_id,
            user_id
        )
    )

    session_summary = (
        summary_info[
            "summary"
        ]
    )

    summarized_through = (
        summary_info[
            "summarized_through_message_id"
        ]
    )

    raw_records = (
        load_message_records(
            conversation_id,
            user_id,
            after_id=
                summarized_through
        )
    )

    messages = []

    if session_summary:

        messages.append({
            "role":
                "system",

            "content":
                (
                    "SESSION CONTEXT SUMMARY:\n"
                    f"{session_summary}\n\n"

                    "This represents older messages "
                    "from the current conversation. "

                    "Recent raw messages take priority "
                    "if there is a conflict."
                )
        })

    for record in raw_records:

        messages.append({
            "role":
                record[
                    "role"
                ],

            "content":
                record[
                    "content"
                ]
        })

    return messages


# =========================================================
# MEMORY MANAGER
# =========================================================

def analyze_memory(
    user_id,
    user_message
):

    relevant_memories = (
        retrieve_relevant_memories(
            user_id,
            user_message,
            limit=
                MEMORY_MANAGER_LIMIT
        )
    )

    memory_list = []

    for memory in relevant_memories:

        memory_list.append({
            "id":
                memory["id"],

            "content":
                memory["content"],

            "category":
                memory["category"],

            "importance":
                memory["importance"],

            "confidence":
                memory["confidence"],

            "source":
                memory["source"],

            "access_count":
                memory["access_count"]
        })

    prompt = """
You manage long-term memory for a private personal AI assistant.

Do NOT answer the user.

Determine whether the newest user message requires
changes to long-term memory.

Possible actions:

CREATE
UPDATE
ARCHIVE
MERGE
NOTHING

Remember durable information such as:

- stable personal facts
- names
- relationships
- preferences
- possessions
- devices
- vehicles
- work information
- ongoing projects
- long-term goals
- recurring habits
- major plans
- durable likes/dislikes
- changes to stored facts

Do not remember:

- casual conversation
- temporary feelings
- random examples
- jokes
- hypothetical statements
- guesses
- quoted information
- passwords
- API keys
- private keys
- security codes
- account numbers

Be conservative.

If new information changes an existing memory,
UPDATE the existing memory rather than creating
a contradiction.

ARCHIVE only when the user clearly indicates
the old fact is obsolete.

MERGE only memories that represent the same
underlying durable fact.

Do not merge merely related memories.

CONFIDENCE:

0.95-1.00 explicitly stated
0.80-0.94 strongly supported
0.60-0.79 somewhat uncertain

Below 0.60:
normally do not store.

IMPORTANCE:

1-3 minor
4-6 useful
7-8 important
9-10 core

Return ONLY valid JSON.

CREATE:

{
  "actions": [
    {
      "action": "create",
      "content": "memory text",
      "category": "personal",
      "importance": 7,
      "confidence": 0.98
    }
  ]
}

UPDATE:

{
  "actions": [
    {
      "action": "update",
      "memory_id": 3,
      "content": "updated text",
      "category": "device",
      "importance": 7,
      "confidence": 0.98
    }
  ]
}

ARCHIVE:

{
  "actions": [
    {
      "action": "archive",
      "memory_id": 3,
      "decision_confidence": 0.95
    }
  ]
}

MERGE:

{
  "actions": [
    {
      "action": "merge",
      "target_memory_id": 3,
      "source_memory_ids": [9],
      "content": "merged memory",
      "category": "device",
      "importance": 7,
      "confidence": 0.98,
      "decision_confidence": 0.97
    }
  ]
}

NOTHING:

{
  "actions": []
}

Normally use no more than 3 actions.

Do not include reasoning.
Do not include Markdown.
"""

    context = {
        "existing_related_memories":
            memory_list,

        "new_user_message":
            user_message
    }

    payload = {
        "model":
            MEMORY_MODEL,

        "messages": [
            {
                "role":
                    "system",

                "content":
                    prompt
            },
            {
                "role":
                    "user",

                "content":
                    json.dumps(
                        context,
                        ensure_ascii=False
                    )
            }
        ],

        "stream":
            False,

        "format":
            "json",

        "options": {
            "temperature": 0
        }
    }

    try:

        response = requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            timeout=300
        )

        response.raise_for_status()

        data = response.json()

        content = (
            data[
                "message"
            ][
                "content"
            ].strip()
        )

        result = json.loads(
            content
        )

        actions = result.get(
            "actions",
            []
        )

        if not isinstance(
            actions,
            list
        ):
            return []

        return actions

    except Exception as error:

        if SHOW_MEMORY_ACTIVITY:

            print(
                "\nMemory manager error:"
                f" {error}\n"
            )

        return []


# =========================================================
# APPLY MEMORY LIFECYCLE
# =========================================================

def process_automatic_memory(
    user_id,
    user_message
):

    if not AUTO_MEMORY:
        return

    if SHOW_MEMORY_ACTIVITY:

        print(
            "Memory: checking...",
            end="",
            flush=True
        )

    actions = analyze_memory(
        user_id,
        user_message
    )

    if SHOW_MEMORY_ACTIVITY:

        print(
            "\r"
            + " " * 80
            + "\r",
            end="",
            flush=True
        )

    changes = []

    current_memories = (
        load_memories(
            user_id
        )
    )

    normalized_existing = {
        memory[1]
        .strip()
        .lower()

        for memory
        in current_memories
    }

    for action_data in actions:

        if not isinstance(
            action_data,
            dict
        ):
            continue

        action = str(
            action_data.get(
                "action",
                ""
            )
        ).lower().strip()

        # -------------------------------------------------
        # CREATE
        # -------------------------------------------------

        if action == "create":

            content = str(
                action_data.get(
                    "content",
                    ""
                )
            ).strip()

            if not content:
                continue

            if (
                content.lower()
                in normalized_existing
            ):
                continue

            category = str(
                action_data.get(
                    "category",
                    "general"
                )
            ).strip().lower()

            importance = (
                clamp_importance(
                    action_data.get(
                        "importance",
                        5
                    )
                )
            )

            confidence = (
                clamp_confidence(
                    action_data.get(
                        "confidence",
                        0.8
                    )
                )
            )

            if confidence < 0.60:
                continue

            embedding = (
                get_embedding(
                    content,
                    show_error=False
                )
            )

            memory_id = (
                save_memory(
                    user_id=
                        user_id,

                    content=
                        content,

                    category=
                        category,

                    importance=
                        importance,

                    confidence=
                        confidence,

                    source=
                        "auto",

                    embedding=
                        embedding
                )
            )

            normalized_existing.add(
                content.lower()
            )

            changes.append(
                f"created #{memory_id}: "
                f"{content}"
            )

        # -------------------------------------------------
        # UPDATE
        # -------------------------------------------------

        elif action == "update":

            try:

                memory_id = int(
                    action_data.get(
                        "memory_id"
                    )
                )

            except (
                ValueError,
                TypeError
            ):
                continue

            existing = (
                get_memory(
                    user_id,
                    memory_id
                )
            )

            if not existing:
                continue

            if (
                existing[6]
                != "active"
            ):
                continue

            content = str(
                action_data.get(
                    "content",
                    ""
                )
            ).strip()

            if not content:
                continue

            category = str(
                action_data.get(
                    "category",
                    existing[2]
                )
            ).strip().lower()

            importance = (
                clamp_importance(
                    action_data.get(
                        "importance",
                        existing[3]
                    )
                )
            )

            confidence = (
                clamp_confidence(
                    action_data.get(
                        "confidence",
                        existing[4]
                    )
                )
            )

            embedding = (
                get_embedding(
                    content,
                    show_error=False
                )
            )

            if update_memory(
                user_id=
                    user_id,

                memory_id=
                    memory_id,

                new_content=
                    content,

                category=
                    category,

                importance=
                    importance,

                confidence=
                    confidence,

                embedding=
                    embedding
            ):

                changes.append(
                    f"updated #{memory_id}: "
                    f"{content}"
                )

        # -------------------------------------------------
        # ARCHIVE
        # -------------------------------------------------

        elif action == "archive":

            try:

                memory_id = int(
                    action_data.get(
                        "memory_id"
                    )
                )

            except (
                ValueError,
                TypeError
            ):
                continue

            decision_confidence = (
                clamp_confidence(
                    action_data.get(
                        "decision_confidence",
                        0
                    )
                )
            )

            if (
                decision_confidence
                <
                AUTO_LIFECYCLE_MIN_CONFIDENCE
            ):
                continue

            existing = (
                get_memory(
                    user_id,
                    memory_id
                )
            )

            if not existing:
                continue

            if (
                existing[6]
                != "active"
            ):
                continue

            if archive_memory(
                user_id,
                memory_id
            ):

                changes.append(
                    f"archived #{memory_id}: "
                    f"{existing[1]}"
                )

        # -------------------------------------------------
        # MERGE
        # -------------------------------------------------

        elif action == "merge":

            decision_confidence = (
                clamp_confidence(
                    action_data.get(
                        "decision_confidence",
                        0
                    )
                )
            )

            if (
                decision_confidence
                <
                AUTO_LIFECYCLE_MIN_CONFIDENCE
            ):
                continue

            try:

                target_id = int(
                    action_data.get(
                        "target_memory_id"
                    )
                )

            except (
                ValueError,
                TypeError
            ):
                continue

            target = get_memory(
                user_id,
                target_id
            )

            if not target:
                continue

            if (
                target[6]
                != "active"
            ):
                continue

            source_ids = (
                action_data.get(
                    "source_memory_ids",
                    []
                )
            )

            if not isinstance(
                source_ids,
                list
            ):
                continue

            clean_source_ids = []

            for source_id in source_ids:

                try:

                    source_id = int(
                        source_id
                    )

                except (
                    ValueError,
                    TypeError
                ):
                    continue

                if source_id == target_id:
                    continue

                source_memory = (
                    get_memory(
                        user_id,
                        source_id
                    )
                )

                if (
                    source_memory
                    and source_memory[6]
                    == "active"
                ):

                    clean_source_ids.append(
                        source_id
                    )

            if not clean_source_ids:
                continue

            content = str(
                action_data.get(
                    "content",
                    target[1]
                )
            ).strip()

            category = str(
                action_data.get(
                    "category",
                    target[2]
                )
            ).strip().lower()

            importance = (
                clamp_importance(
                    action_data.get(
                        "importance",
                        target[3]
                    )
                )
            )

            memory_confidence = (
                clamp_confidence(
                    action_data.get(
                        "confidence",
                        target[4]
                    )
                )
            )

            embedding = (
                get_embedding(
                    content,
                    show_error=False
                )
            )

            updated = update_memory(
                user_id=
                    user_id,

                memory_id=
                    target_id,

                new_content=
                    content,

                category=
                    category,

                importance=
                    importance,

                confidence=
                    memory_confidence,

                embedding=
                    embedding
            )

            if not updated:
                continue

            archived_sources = []

            for source_id in clean_source_ids:

                if archive_memory(
                    user_id,
                    source_id,
                    merged_into_id=
                        target_id
                ):

                    archived_sources.append(
                        source_id
                    )

            if archived_sources:

                changes.append(
                    f"merged "
                    f"{archived_sources} "
                    f"into #{target_id}: "
                    f"{content}"
                )

    if SHOW_MEMORY_ACTIVITY:

        if changes:

            for change in changes:

                print(
                    f"Memory: {change}"
                )

            print()

        else:

            print(
                "Memory: no long-term "
                "change.\n"
            )


# =========================================================
# MAIN CHAT
# =========================================================

def ask_ai(
    user_id,
    conversation_id,
    message
):

    save_message(
        conversation_id,
        user_id,
        "user",
        message
    )

    maybe_update_session_summary(
        user_id,
        conversation_id
    )

    messages = (
        build_session_context(
            user_id,
            conversation_id
        )
    )

    relevant_memories = (
        retrieve_relevant_memories(
            user_id,
            message,
            limit=
                MEMORY_RETRIEVAL_LIMIT
        )
    )

    if relevant_memories:

        mark_memories_accessed(
            user_id,
            [
                memory[
                    "id"
                ]

                for memory
                in relevant_memories
            ]
        )

        memory_text = "\n".join(
            (
                f"- "
                f"[{memory['category']} | "
                f"importance "
                f"{memory['importance']} | "
                f"confidence "
                f"{memory['confidence']:.2f}] "
                f"{memory['content']}"
            )

            for memory
            in relevant_memories
        )

        messages.insert(
            0,
            {
                "role":
                    "system",

                "content":
                    (
                        "RELEVANT LONG-TERM MEMORY:\n"
                        f"{memory_text}\n\n"

                        "Use these memories naturally "
                        "when helpful. "

                        "Ignore irrelevant memories. "

                        "Recent explicit user statements "
                        "take priority over older memory."
                    )
            }
        )

    # -----------------------------------------------------
    # Same identity/personality instruction
    # regardless of which model answers.
    # -----------------------------------------------------

    messages.insert(
        0,
        {
            "role":
                "system",

            "content":
                (
                    "You are one consistent private personal "
                    "AI assistant. "

                    "Respond naturally, clearly, and helpfully. "

                    "Do not change personality based on the "
                    "underlying model being used. "

                    "Use relevant session context and long-term "
                    "memory when available. "

                    "Recent user statements take priority over "
                    "older summaries or memories."
                )
        }
    )

    # -----------------------------------------------------
    # Select model
    # -----------------------------------------------------

    selected_mode, selected_model = (
        route_model(
            message
        )
    )

    if SHOW_ROUTER_ACTIVITY:

        print(
            f"Router: "
            f"{selected_mode} "
            f"→ {selected_model}"
        )

    payload = {
        "model":
            selected_model,

        "messages":
            messages,

        "stream":
            True
    }

    print(
        "AI: ",
        end="",
        flush=True
    )

    full_response = ""

    try:

        with requests.post(
            OLLAMA_CHAT_URL,
            json=payload,
            stream=True,
            timeout=600
        ) as response:

            response.raise_for_status()

            for line in response.iter_lines():

                if not line:
                    continue

                data = json.loads(
                    line
                )

                if "message" in data:

                    chunk = (
                        data[
                            "message"
                        ].get(
                            "content",
                            ""
                        )
                    )

                    full_response += (
                        chunk
                    )

                    print(
                        chunk,
                        end="",
                        flush=True
                    )

        print("\n")

        save_message(
            conversation_id,
            user_id,
            "assistant",
            full_response
        )

        process_automatic_memory(
            user_id,
            message
        )

    except requests.exceptions.ConnectionError:

        print(
            "\n\nError: Could not connect "
            "to Ollama.\n"
        )

    except requests.exceptions.RequestException as error:

        print(
            f"\n\nOllama request error: "
            f"{error}\n"
        )

    except json.JSONDecodeError:

        print(
            "\n\nError reading response "
            "from Ollama.\n"
        )


# =========================================================
# USER DISPLAY
# =========================================================

def show_users():

    users = list_users()

    print(
        "\nUsers:"
    )

    for user in users:

        print(
            f"{user[0]} | "
            f"{user[1]} | "
            f"{user[2]} | "
            f"role {user[3]} | "
            f"{user[4]}"
        )

    print()


def show_current_user(
    user_id
):

    user = get_user(
        user_id
    )

    if not user:

        print(
            "\nCurrent user not found.\n"
        )

        return

    print(
        f"\nCurrent user: "
        f"{user[2]}"
    )

    print(
        f"ID: {user[0]}"
    )

    print(
        f"Username: {user[1]}"
    )

    print(
        f"Role: {user[3]}"
    )

    print(
        f"Status: {user[4]}\n"
    )


# =========================================================
# MODEL DISPLAY
# =========================================================

def show_model_mode():

    print(
        f"\nCurrent model mode: "
        f"{MODEL_MODE}"
    )

    print(
        f"Fast:    {FAST_MODEL}"
    )

    print(
        f"Default: {DEFAULT_MODEL}"
    )

    print(
        f"Deep:    {DEEP_MODEL}"
    )

    print()


# =========================================================
# SESSION DISPLAY
# =========================================================

def show_sessions(
    user_id
):

    conversations = (
        list_conversations(
            user_id
        )
    )

    if not conversations:

        print(
            "\nNo previous chats.\n"
        )

        return

    print(
        "\nRecent chats:"
    )

    for conversation in conversations:

        print(
            f"{conversation[0]}: "
            f"{conversation[1]} "
            f"({conversation[2]})"
        )

    print()


def show_session_summary(
    user_id,
    conversation_id
):

    info = (
        get_conversation_summary(
            conversation_id,
            user_id
        )
    )

    if not info[
        "summary"
    ]:

        print(
            "\nThis session has not "
            "needed summarization yet.\n"
        )

        return

    print(
        "\nSession summary:\n"
    )

    print(
        info[
            "summary"
        ]
    )

    print(
        "\nSummarized through message:"
        f" "
        f"{info['summarized_through_message_id']}"
    )

    print(
        "Updated:"
        f" "
        f"{info['summary_updated_at']}\n"
    )


# =========================================================
# MEMORY DISPLAY
# =========================================================

def show_memories(
    user_id,
    include_archived=False
):

    memories = load_memories(
        user_id,
        include_archived=
            include_archived
    )

    if not memories:

        print(
            "\nNo memories found.\n"
        )

        return

    if include_archived:

        print(
            "\nAll memories:"
        )

    else:

        print(
            "\nActive memories:"
        )

    for memory in memories:

        print(
            f"{memory[0]} | "
            f"{memory[6]} | "
            f"{memory[2]} | "
            f"importance "
            f"{memory[3]} | "
            f"confidence "
            f"{memory[4]:.2f} | "
            f"uses "
            f"{memory[10]} | "
            f"{memory[1]}"
        )

        if (
            memory[6]
            == "archived"
            and memory[12]
        ):

            print(
                f"    merged into "
                f"memory #{memory[12]}"
            )

    print()


def show_memory_details(
    user_id,
    memory_id
):

    memory = get_memory(
        user_id,
        memory_id
    )

    if not memory:

        print(
            "\nMemory not found.\n"
        )

        return

    print(
        f"\nMemory #{memory[0]}"
    )

    print(
        f"Content: "
        f"{memory[1]}"
    )

    print(
        f"Category: "
        f"{memory[2]}"
    )

    print(
        f"Importance: "
        f"{memory[3]}"
    )

    print(
        f"Confidence: "
        f"{memory[4]:.2f}"
    )

    print(
        f"Source: "
        f"{memory[5]}"
    )

    print(
        f"Status: "
        f"{memory[6]}"
    )

    print(
        f"Created: "
        f"{memory[7]}"
    )

    print(
        f"Updated: "
        f"{memory[8]}"
    )

    print(
        f"Last accessed: "
        f"{memory[9]}"
    )

    print(
        f"Access count: "
        f"{memory[10]}"
    )

    print(
        f"Merged into: "
        f"{memory[12]}\n"
    )


def show_relevant_memories(
    user_id,
    query
):

    results = (
        retrieve_relevant_memories(
            user_id,
            query,
            limit=10
        )
    )

    if not results:

        print(
            "\nNo memories found.\n"
        )

        return

    print(
        "\nMost relevant memories:"
    )

    for memory in results:

        print(
            f"{memory['id']} | "
            f"score "
            f"{memory['semantic_score']:.3f} | "
            f"confidence "
            f"{memory['confidence']:.2f} | "
            f"{memory['category']} | "
            f"{memory['content']}"
        )

    print()


# =========================================================
# STARTUP
# =========================================================

current_user_id = (
    initialize_database()
)

ensure_memory_embeddings()

current_user = get_user(
    current_user_id
)

conversation_id = (
    create_conversation(
        current_user_id
    )
)


print(
    "\nPrivate AI started."
)

print(
    f"Current user: "
    f"{current_user[2]}"
)

print(
    f"Current chat session: "
    f"{conversation_id}"
)

print(
    f"Model mode: "
    f"{MODEL_MODE}"
)


print("""
Commands:

 MODEL

 /model
     Show current model mode

 /model auto
     Automatically select model

 /model fast
     Force Qwen3 4B

 /model default
     Force Qwen3 8B

 /model deep
     Force DeepSeek-R1 14B


 USER

 /whoami
     Show current user

 /users
     Show users

 /user add USERNAME DISPLAY_NAME
     Create a user

 /switch ID
     Switch active user


 CHAT

 /new
     Start a new chat

 /sessions
     Show this user's chats

 /resume ID
     Resume this user's chat

 /summary
     Show current session summary


 MEMORY

 /remember TEXT
     Manually save memory

 /memories
     Show active memories

 /memories all
     Show active + archived memories

 /memory ID
     Show memory lifecycle details

 /edit ID TEXT
     Edit memory

 /archive ID
     Archive memory

 /restore ID
     Restore memory

 /forget ID
     Permanently delete memory

 /relevant TEXT
     Test semantic retrieval

 /reindex
     Rebuild memory embeddings


 SYSTEM

 /exit
     Quit


Automatic memory: ON
Semantic retrieval: ON
Session summarization: ON
Memory lifecycle: ON
Multi-user foundation: ON
Multi-model routing: ON

Authentication: NOT YET ENABLED
""")


# =========================================================
# MAIN LOOP
# =========================================================

while True:

    try:

        user_input = input(
            "You: "
        ).strip()

    except KeyboardInterrupt:

        print(
            "\n\nPrivate AI stopped.\n"
        )

        break

    if not user_input:
        continue

    command = (
        user_input.lower()
    )


    # =====================================================
    # EXIT
    # =====================================================

    if command in [
        "/exit",
        "exit",
        "quit"
    ]:

        print(
            "\nPrivate AI stopped.\n"
        )

        break


    # =====================================================
    # MODEL
    # =====================================================

    if command == "/model":

        show_model_mode()

        continue


    if command.startswith(
        "/model "
    ):

        requested_mode = (
            user_input.split(
                maxsplit=1
            )[1]
            .lower()
            .strip()
        )

        if requested_mode not in [
            "auto",
            "fast",
            "default",
            "deep"
        ]:

            print(
                "\nUse one of:\n"
                "/model auto\n"
                "/model fast\n"
                "/model default\n"
                "/model deep\n"
            )

            continue

        MODEL_MODE = (
            requested_mode
        )

        print(
            f"\nModel mode changed "
            f"to: {MODEL_MODE}\n"
        )

        continue


    # =====================================================
    # WHOAMI
    # =====================================================

    if command == "/whoami":

        show_current_user(
            current_user_id
        )

        continue


    # =====================================================
    # USERS
    # =====================================================

    if command == "/users":

        show_users()

        continue


    # =====================================================
    # CREATE USER
    # =====================================================

    if command.startswith(
        "/user add"
    ):

        parts = (
            user_input.split(
                maxsplit=3
            )
        )

        if len(parts) < 3:

            print(
                "\nUse:\n"
                "/user add USERNAME DISPLAY_NAME\n"
            )

            continue

        username = parts[2]

        if len(parts) == 4:

            display_name = (
                parts[3]
            )

        else:

            display_name = (
                username
            )

        new_user_id = create_user(
            username=
                username,

            display_name=
                display_name
        )

        if new_user_id:

            print(
                f"\nCreated user "
                f"#{new_user_id}: "
                f"{display_name}\n"
            )

        else:

            print(
                "\nCould not create user. "
                "Username may already exist.\n"
            )

        continue


    # =====================================================
    # SWITCH USER
    # =====================================================

    if command.startswith(
        "/switch"
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /switch 2\n"
            )

            continue

        try:

            requested_user_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nUser ID must be "
                "a number.\n"
            )

            continue

        requested_user = get_user(
            requested_user_id
        )

        if not requested_user:

            print(
                "\nUser not found.\n"
            )

            continue

        if (
            requested_user[4]
            != "active"
        ):

            print(
                "\nUser is not active.\n"
            )

            continue

        current_user_id = (
            requested_user_id
        )

        current_user = (
            requested_user
        )

        conversation_id = (
            create_conversation(
                current_user_id
            )
        )

        print(
            f"\nSwitched to: "
            f"{current_user[2]}"
        )

        print(
            f"New chat session: "
            f"{conversation_id}\n"
        )

        continue


    # =====================================================
    # NEW CHAT
    # =====================================================

    if command == "/new":

        conversation_id = (
            create_conversation(
                current_user_id
            )
        )

        print(
            f"\nStarted new chat "
            f"{conversation_id}\n"
        )

        continue


    # =====================================================
    # SESSIONS
    # =====================================================

    if command == "/sessions":

        show_sessions(
            current_user_id
        )

        continue


    # =====================================================
    # SUMMARY
    # =====================================================

    if command == "/summary":

        show_session_summary(
            current_user_id,
            conversation_id
        )

        continue


    # =====================================================
    # RESUME
    # =====================================================

    if command.startswith(
        "/resume"
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /resume 3\n"
            )

            continue

        try:

            requested_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nSession ID must "
                "be a number.\n"
            )

            continue

        if not conversation_belongs_to_user(
            requested_id,
            current_user_id
        ):

            print(
                "\nChat not found for "
                "current user.\n"
            )

            continue

        messages = (
            load_messages(
                requested_id,
                current_user_id
            )
        )

        if messages:

            conversation_id = (
                requested_id
            )

            print(
                f"\nResumed chat "
                f"{conversation_id}\n"
            )

            maybe_update_session_summary(
                current_user_id,
                conversation_id
            )

        else:

            print(
                "\nChat contains "
                "no messages.\n"
            )

        continue


    # =====================================================
    # REMEMBER
    # =====================================================

    if command.startswith(
        "/remember"
    ):

        memory_text = user_input[
            len("/remember"):
        ].strip()

        if not memory_text:

            print(
                "\nUse: /remember "
                "something important\n"
            )

            continue

        embedding = (
            get_embedding(
                memory_text
            )
        )

        memory_id = (
            save_memory(
                user_id=
                    current_user_id,

                content=
                    memory_text,

                category=
                    "general",

                importance=
                    10,

                confidence=
                    1.0,

                source=
                    "manual",

                embedding=
                    embedding
            )
        )

        print(
            f"\nRemembered "
            f"#{memory_id}: "
            f"{memory_text}\n"
        )

        continue


    # =====================================================
    # MEMORIES
    # =====================================================

    if command == "/memories":

        show_memories(
            current_user_id,
            include_archived=False
        )

        continue


    if command == "/memories all":

        show_memories(
            current_user_id,
            include_archived=True
        )

        continue


    # =====================================================
    # MEMORY DETAILS
    # =====================================================

    if command.startswith(
        "/memory "
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /memory 3\n"
            )

            continue

        try:

            memory_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nMemory ID must "
                "be a number.\n"
            )

            continue

        show_memory_details(
            current_user_id,
            memory_id
        )

        continue


    # =====================================================
    # EDIT MEMORY
    # =====================================================

    if command.startswith(
        "/edit"
    ):

        parts = (
            user_input.split(
                maxsplit=2
            )
        )

        if len(parts) != 3:

            print(
                "\nUse: "
                "/edit 2 New memory text\n"
            )

            continue

        try:

            memory_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nMemory ID must "
                "be a number.\n"
            )

            continue

        new_text = (
            parts[2].strip()
        )

        new_embedding = (
            get_embedding(
                new_text
            )
        )

        if update_memory(
            user_id=
                current_user_id,

            memory_id=
                memory_id,

            new_content=
                new_text,

            confidence=
                1.0,

            source=
                "manual",

            embedding=
                new_embedding
        ):

            print(
                f"\nUpdated memory "
                f"{memory_id}.\n"
            )

        else:

            print(
                "\nMemory not found "
                "for current user.\n"
            )

        continue


    # =====================================================
    # ARCHIVE
    # =====================================================

    if command.startswith(
        "/archive"
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /archive 3\n"
            )

            continue

        try:

            memory_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nMemory ID must "
                "be a number.\n"
            )

            continue

        if archive_memory(
            current_user_id,
            memory_id
        ):

            print(
                f"\nArchived memory "
                f"{memory_id}.\n"
            )

        else:

            print(
                "\nMemory not found.\n"
            )

        continue


    # =====================================================
    # RESTORE
    # =====================================================

    if command.startswith(
        "/restore"
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /restore 3\n"
            )

            continue

        try:

            memory_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nMemory ID must "
                "be a number.\n"
            )

            continue

        if restore_memory(
            current_user_id,
            memory_id
        ):

            print(
                f"\nRestored memory "
                f"{memory_id}.\n"
            )

        else:

            print(
                "\nMemory not found.\n"
            )

        continue


    # =====================================================
    # FORGET
    # =====================================================

    if command.startswith(
        "/forget"
    ):

        parts = (
            user_input.split()
        )

        if len(parts) != 2:

            print(
                "\nUse: /forget 2\n"
            )

            continue

        try:

            memory_id = int(
                parts[1]
            )

        except ValueError:

            print(
                "\nMemory ID must "
                "be a number.\n"
            )

            continue

        if delete_memory(
            current_user_id,
            memory_id
        ):

            print(
                f"\nPermanently deleted "
                f"memory {memory_id}.\n"
            )

        else:

            print(
                "\nMemory not found.\n"
            )

        continue


    # =====================================================
    # RELEVANT
    # =====================================================

    if command.startswith(
        "/relevant"
    ):

        query = user_input[
            len("/relevant"):
        ].strip()

        if not query:

            print(
                "\nUse: /relevant "
                "camera equipment\n"
            )

            continue

        show_relevant_memories(
            current_user_id,
            query
        )

        continue


    # =====================================================
    # REINDEX
    # =====================================================

    if command == "/reindex":

        rebuild_memory_embeddings()

        continue


    # =====================================================
    # NORMAL CHAT
    # =====================================================

    ask_ai(
        current_user_id,
        conversation_id,
        user_input
    )