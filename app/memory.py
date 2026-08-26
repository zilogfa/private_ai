import json
import math

from app.config import (
    AUTO_MEMORY,
    SHOW_MEMORY_ACTIVITY,
    MEMORY_RETRIEVAL_LIMIT,
    MEMORY_MANAGER_LIMIT,
    AUTO_LIFECYCLE_MIN_CONFIDENCE,
    MEMORY_MODEL,
)

from app.ollama_client import (
    chat_once,
    get_embedding,
)

from app.database import (
    save_memory,
    load_memories,
    get_memory,
    update_memory,
    archive_memory,
    mark_memories_accessed,
    load_memories_without_embeddings,
    set_memory_embedding,
    clear_memory_embeddings,
)


# =========================================================
# HELPERS
# =========================================================

def clamp_importance(value):
    try:
        value = int(value)
    except (ValueError, TypeError):
        return 5

    return max(1, min(10, value))


def clamp_confidence(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        return 0.7

    return max(0.0, min(1.0, value))


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
        "merged_into_id": memory[12],
    }


# =========================================================
# EMBEDDING HELPERS
# =========================================================

def parse_embedding(stored_embedding):
    if stored_embedding is None:
        return None

    if isinstance(stored_embedding, list):
        return stored_embedding

    try:
        return json.loads(stored_embedding)
    except (json.JSONDecodeError, TypeError):
        return None


def cosine_similarity(vector_a, vector_b):
    if not vector_a or not vector_b:
        return 0.0

    if len(vector_a) != len(vector_b):
        return 0.0

    dot_product = sum(
        a * b
        for a, b in zip(vector_a, vector_b)
    )

    magnitude_a = math.sqrt(
        sum(a * a for a in vector_a)
    )

    magnitude_b = math.sqrt(
        sum(b * b for b in vector_b)
    )

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return (
        dot_product
        /
        (magnitude_a * magnitude_b)
    )


# =========================================================
# EMBEDDING INDEX
# =========================================================

def ensure_memory_embeddings():
    missing = load_memories_without_embeddings()

    if not missing:
        return

    print(
        f"\nIndexing {len(missing)} memories..."
    )

    completed = 0

    for memory_id, content in missing:
        embedding = get_embedding(content)

        if embedding:
            set_memory_embedding(
                memory_id,
                embedding,
            )

            completed += 1

    print(
        f"Memory index ready: "
        f"{completed}/{len(missing)} indexed.\n"
    )


def rebuild_memory_embeddings():
    print("\nRebuilding memory index...")

    clear_memory_embeddings()
    ensure_memory_embeddings()


# =========================================================
# SEMANTIC MEMORY RETRIEVAL
# =========================================================

def retrieve_relevant_memories(
    user_id,
    query,
    limit=MEMORY_RETRIEVAL_LIMIT,
):
    memories = load_memories(user_id)

    if not memories:
        return []

    query_embedding = get_embedding(
        query,
        show_error=False,
    )

    if not query_embedding:
        results = []

        for memory in memories[:limit]:
            item = memory_to_dict(memory)

            item["semantic_score"] = 0
            item["ranking_score"] = 0

            results.append(item)

        return results

    scored = []

    for memory in memories:
        item = memory_to_dict(memory)

        memory_embedding = parse_embedding(
            item["embedding"]
        )

        if not memory_embedding:
            continue

        similarity = cosine_similarity(
            query_embedding,
            memory_embedding,
        )

        importance_bonus = (
            item["importance"] / 10
        ) * 0.04

        confidence_bonus = (
            item["confidence"]
        ) * 0.03

        ranking_score = (
            similarity
            + importance_bonus
            + confidence_bonus
        )

        item["semantic_score"] = similarity
        item["ranking_score"] = ranking_score

        scored.append(item)

    scored.sort(
        key=lambda item: item["ranking_score"],
        reverse=True,
    )

    return scored[:limit]


# =========================================================
# MEMORY MANAGER
# =========================================================

def analyze_memory(
    user_id,
    user_message,
):
    relevant_memories = retrieve_relevant_memories(
        user_id,
        user_message,
        limit=MEMORY_MANAGER_LIMIT,
    )

    memory_list = []

    for memory in relevant_memories:
        memory_list.append({
            "id": memory["id"],
            "content": memory["content"],
            "category": memory["category"],
            "importance": memory["importance"],
            "confidence": memory["confidence"],
            "source": memory["source"],
            "access_count": memory["access_count"],
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
        "existing_related_memories": memory_list,
        "new_user_message": user_message,
    }

    messages = [
        {
            "role": "system",
            "content": prompt,
        },
        {
            "role": "user",
            "content": json.dumps(
                context,
                ensure_ascii=False,
            ),
        },
    ]

    try:
        data = chat_once(
            model=MEMORY_MODEL,
            messages=messages,
            response_format="json",
            options={
                "temperature": 0,
            },
            timeout=300,
        )

        content = (
            data
            .get("message", {})
            .get("content", "")
            .strip()
        )

        result = json.loads(content)

        actions = result.get(
            "actions",
            [],
        )

        if not isinstance(actions, list):
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
    user_message,
):
    if not AUTO_MEMORY:
        return

    if SHOW_MEMORY_ACTIVITY:
        print(
            "Memory: checking...",
            end="",
            flush=True,
        )

    actions = analyze_memory(
        user_id,
        user_message,
    )

    if SHOW_MEMORY_ACTIVITY:
        print(
            "\r"
            + " " * 80
            + "\r",
            end="",
            flush=True,
        )

    changes = []

    current_memories = load_memories(
        user_id
    )

    normalized_existing = {
        memory[1].strip().lower()
        for memory in current_memories
    }

    for action_data in actions:
        if not isinstance(
            action_data,
            dict,
        ):
            continue

        action = str(
            action_data.get(
                "action",
                "",
            )
        ).lower().strip()

        # -------------------------------------------------
        # CREATE
        # -------------------------------------------------

        if action == "create":
            content = str(
                action_data.get(
                    "content",
                    "",
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
                    "general",
                )
            ).strip().lower()

            importance = clamp_importance(
                action_data.get(
                    "importance",
                    5,
                )
            )

            confidence = clamp_confidence(
                action_data.get(
                    "confidence",
                    0.8,
                )
            )

            if confidence < 0.60:
                continue

            embedding = get_embedding(
                content,
                show_error=False,
            )

            memory_id = save_memory(
                user_id=user_id,
                content=content,
                category=category,
                importance=importance,
                confidence=confidence,
                source="auto",
                embedding=embedding,
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
            except (ValueError, TypeError):
                continue

            existing = get_memory(
                user_id,
                memory_id,
            )

            if not existing:
                continue

            if existing[6] != "active":
                continue

            content = str(
                action_data.get(
                    "content",
                    "",
                )
            ).strip()

            if not content:
                continue

            category = str(
                action_data.get(
                    "category",
                    existing[2],
                )
            ).strip().lower()

            importance = clamp_importance(
                action_data.get(
                    "importance",
                    existing[3],
                )
            )

            confidence = clamp_confidence(
                action_data.get(
                    "confidence",
                    existing[4],
                )
            )

            embedding = get_embedding(
                content,
                show_error=False,
            )

            if update_memory(
                user_id=user_id,
                memory_id=memory_id,
                new_content=content,
                category=category,
                importance=importance,
                confidence=confidence,
                embedding=embedding,
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
            except (ValueError, TypeError):
                continue

            decision_confidence = (
                clamp_confidence(
                    action_data.get(
                        "decision_confidence",
                        0,
                    )
                )
            )

            if (
                decision_confidence
                <
                AUTO_LIFECYCLE_MIN_CONFIDENCE
            ):
                continue

            existing = get_memory(
                user_id,
                memory_id,
            )

            if not existing:
                continue

            if existing[6] != "active":
                continue

            if archive_memory(
                user_id,
                memory_id,
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
                        0,
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
            except (ValueError, TypeError):
                continue

            target = get_memory(
                user_id,
                target_id,
            )

            if not target:
                continue

            if target[6] != "active":
                continue

            source_ids = action_data.get(
                "source_memory_ids",
                [],
            )

            if not isinstance(
                source_ids,
                list,
            ):
                continue

            clean_source_ids = []

            for source_id in source_ids:
                try:
                    source_id = int(
                        source_id
                    )
                except (ValueError, TypeError):
                    continue

                if source_id == target_id:
                    continue

                source_memory = get_memory(
                    user_id,
                    source_id,
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
                    target[1],
                )
            ).strip()

            category = str(
                action_data.get(
                    "category",
                    target[2],
                )
            ).strip().lower()

            importance = clamp_importance(
                action_data.get(
                    "importance",
                    target[3],
                )
            )

            memory_confidence = (
                clamp_confidence(
                    action_data.get(
                        "confidence",
                        target[4],
                    )
                )
            )

            embedding = get_embedding(
                content,
                show_error=False,
            )

            updated = update_memory(
                user_id=user_id,
                memory_id=target_id,
                new_content=content,
                category=category,
                importance=importance,
                confidence=memory_confidence,
                embedding=embedding,
            )

            if not updated:
                continue

            archived_sources = []

            for source_id in clean_source_ids:
                if archive_memory(
                    user_id,
                    source_id,
                    merged_into_id=target_id,
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