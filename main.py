from app.config import (
    FAST_MODEL,
    DEFAULT_MODEL,
    DEEP_MODEL,
    SHOW_ROUTER_ACTIVITY,
)

from app.ollama_client import (
    get_embedding,
)

from app.router import (
    get_model_mode,
    set_model_mode,
)

from app.database import (
    initialize_database,

    create_user,
    get_user,
    list_users,

    create_conversation,
    list_conversations,
    conversation_belongs_to_user,

    load_messages,

    get_conversation_summary,

    save_memory,
    load_memories,
    get_memory,
    update_memory,
    archive_memory,
    restore_memory,
    delete_memory,
)

from app.memory import (
    ensure_memory_embeddings,
    rebuild_memory_embeddings,
    retrieve_relevant_memories,
)

from app.sessions import (
    maybe_update_session_summary,
)

from app.services.chat import (
    stream_chat,
)


# =========================================================
# TERMINAL CHAT ADAPTER
# =========================================================

def ask_ai(
    user_id,
    conversation_id,
    message,
):
    """
    Terminal adapter for the shared ChatService.

    The backend itself lives in:

        app/services/chat.py

    Future Flask routes will consume the exact same
    stream_chat() function.
    """

    response_started = False

    for event in stream_chat(
        user_id,
        conversation_id,
        message,
    ):
        event_type = event.get(
            "type"
        )

        # -------------------------------------------------
        # ROUTING
        # -------------------------------------------------

        if event_type == "route":

            if SHOW_ROUTER_ACTIVITY:
                print(
                    f"Router: "
                    f"{event['mode']} "
                    f"→ {event['model']}"
                )

            print(
                "AI: ",
                end="",
                flush=True,
            )

            response_started = True

        # -------------------------------------------------
        # THINKING
        #
        # Intentionally hidden in terminal for now.
        # The backend already exposes it for the future
        # web interface.
        # -------------------------------------------------

        elif event_type == "thinking":
            continue

        # -------------------------------------------------
        # RESPONSE CONTENT
        # -------------------------------------------------

        elif event_type == "content":

            if not response_started:
                print(
                    "AI: ",
                    end="",
                    flush=True,
                )

                response_started = True

            print(
                event.get(
                    "content",
                    "",
                ),
                end="",
                flush=True,
            )

        # -------------------------------------------------
        # VISIBLE RESPONSE COMPLETE
        # -------------------------------------------------

        elif event_type == "response_complete":

            print("\n")

        # -------------------------------------------------
        # ERROR
        # -------------------------------------------------

        elif event_type == "error":

            message_text = event.get(
                "message",
                "Unknown chat error.",
            )

            print(
                f"\n\nError: "
                f"{message_text}\n"
            )


# =========================================================
# USER DISPLAY
# =========================================================

def show_users():
    users = list_users()

    print("\nUsers:")

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
    user_id,
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
        f"{get_model_mode()}"
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
    user_id,
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
    conversation_id,
):
    info = (
        get_conversation_summary(
            conversation_id,
            user_id,
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
    include_archived=False,
):
    memories = load_memories(
        user_id,
        include_archived=
            include_archived,
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
    memory_id,
):
    memory = get_memory(
        user_id,
        memory_id,
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
    query,
):
    results = (
        retrieve_relevant_memories(
            user_id,
            query,
            limit=10,
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
    f"{get_model_mode()}"
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
Shared chat service: ON

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
        "quit",
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
            user_input
            .split(
                maxsplit=1
            )[1]
            .lower()
            .strip()
        )

        if not set_model_mode(
            requested_mode
        ):
            print(
                "\nUse one of:\n"
                "/model auto\n"
                "/model fast\n"
                "/model default\n"
                "/model deep\n"
            )

            continue

        print(
            f"\nModel mode changed "
            f"to: {get_model_mode()}\n"
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
            display_name = parts[3]

        else:
            display_name = username

        new_user_id = create_user(
            username=username,
            display_name=display_name,
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
            conversation_id,
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
            current_user_id,
        ):
            print(
                "\nChat not found for "
                "current user.\n"
            )

            continue

        messages = load_messages(
            requested_id,
            current_user_id,
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
                conversation_id,
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

        embedding = get_embedding(
            memory_text
        )

        memory_id = save_memory(
            user_id=current_user_id,
            content=memory_text,
            category="general",
            importance=10,
            confidence=1.0,
            source="manual",
            embedding=embedding,
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
            include_archived=False,
        )

        continue


    if command == "/memories all":
        show_memories(
            current_user_id,
            include_archived=True,
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
            memory_id,
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

        new_embedding = get_embedding(
            new_text
        )

        if update_memory(
            user_id=current_user_id,
            memory_id=memory_id,
            new_content=new_text,
            confidence=1.0,
            source="manual",
            embedding=new_embedding,
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
            memory_id,
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
            memory_id,
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
            memory_id,
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
            query,
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
        user_input,
    )