"""Integration checks against real PostgreSQL; no SQLite stand-in or mocked locks."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from clinic_agent.agent import SchedulingService
from clinic_agent.domain import operation_digest
from clinic_agent.models import BookingProposal, Constraints, Principal
from clinic_agent.storage import ClinicStore

NOW = datetime.fromisoformat("2026-10-07T09:00:00+05:30")
PATIENT = Principal()


def services(urls, count=2):
    schedulers = [ClinicStore(urls["scheduling"], NOW) for _ in range(count)]
    agents = [
        SchedulingService(
            urls["conversation"],
            checkpoint_path=urls["conversation"],
            scheduler=store,
            fixed_now=NOW,
        )
        for store in schedulers
    ]
    return agents, schedulers


def prepare(agent):
    response = agent.create_conversation(PATIENT)
    response = agent.message(
        response.conversation_id, "request", "I need primary care tomorrow after 3 pm", PATIENT
    )
    return agent.message(
        response.conversation_id,
        "select",
        "",
        PATIENT,
        action="select_slot",
        slot_id=response.options[0].slot_id,
    )


def close_all(agents, schedulers):
    for agent in agents:
        agent.close()
    for scheduler in schedulers:
        scheduler.close()


def test_postgres_replica_duplicate_confirmation_is_serialized(postgres_urls):
    agents, schedulers = services(postgres_urls)
    try:
        response = prepare(agents[0])
        barrier = threading.Barrier(2)

        def confirm(agent):
            barrier.wait(timeout=5)
            return agent.message(
                response.conversation_id,
                "same-confirmation",
                "Yes, I confirm this exact appointment.",
                PATIENT,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, agents))
        assert results[0] == results[1]
        assert all(result.status == "booked" for result in results)
        assert len(schedulers[0].inspect_world()["appointments"]) == 1
        assert len(schedulers[0].inspect_world()["operations"]) == 1
        with agents[0].store.lock:
            assert (
                agents[0]
                .store.connection.execute(
                    "SELECT COUNT(*) FROM turns WHERE conversation_id=?",
                    (response.conversation_id,),
                )
                .fetchone()[0]
                == 3
            )
    finally:
        close_all(agents, schedulers)


def test_postgres_concurrent_slot_capacity_and_operation_replay(postgres_urls):
    stores = [ClinicStore(postgres_urls["scheduling"], NOW) for _ in range(2)]
    state = {
        "conversation_id": "race",
        "tenant_id": "demo-clinic",
        "patient_id": "patient-maya",
        "request_revision": 1,
        "constraints": Constraints(appointment_type="primary-care", date="2026-10-08").model_dump(),
    }
    slot = stores[0].search(PATIENT, Constraints.model_validate(state["constraints"]), state, NOW)[
        0
    ]
    barrier = threading.Barrier(2)

    def attempt(pair):
        store, patient_id = pair
        identity = Principal(patient_id=patient_id)
        proposal = BookingProposal(
            proposal_id="proposal-" + patient_id,
            request_revision=1,
            patient_id=patient_id,
            slot=slot,
            expires_at=(NOW + timedelta(minutes=10)).isoformat(),
        )
        barrier.wait(timeout=5)
        result = store.book(
            identity,
            proposal,
            "op-" + patient_id,
            operation_digest(identity, proposal),
            {**state, "patient_id": patient_id},
            NOW,
        )
        return result, identity, proposal

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(attempt, [(stores[0], "patient-maya"), (stores[1], "patient-arjun")])
            )
        assert sorted(result[0]["status"] for result in results) == ["BOOKED", "SLOT_UNAVAILABLE"]
        booked, identity, proposal = next(
            result for result in results if result[0]["status"] == "BOOKED"
        )
        replay = stores[1].book(
            identity,
            proposal,
            "op-" + identity.patient_id,
            operation_digest(identity, proposal),
            {**state, "patient_id": identity.patient_id},
            NOW + timedelta(hours=1),
        )
        assert replay == booked
        assert len(stores[0].inspect_world()["appointments"]) == 1
    finally:
        for store in stores:
            store.close()


def test_postgres_crash_after_scheduler_commit_recovers_from_other_replica(postgres_urls):
    agents, schedulers = services(postgres_urls)
    try:
        response = prepare(agents[0])
        original = schedulers[0].book

        def commit_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Injected crash after commit")

        schedulers[0].book = commit_then_crash
        with pytest.raises(RuntimeError):
            agents[0].message(response.conversation_id, "confirm-crash", "yes", PATIENT)
        recovered = agents[1].message(response.conversation_id, "confirm-crash", "yes", PATIENT)
        assert recovered.status == "booked"
        assert len(schedulers[1].inspect_world()["appointments"]) == 1
        assert len(schedulers[1].inspect_world()["operations"]) == 1
        snapshot = agents[1].graph.get_state(
            {"configurable": {"thread_id": f"demo-clinic:{response.conversation_id}"}}
        )
        assert snapshot.values["conversation"]["status"] == "booked"
    finally:
        close_all(agents, schedulers)


def test_postgres_shared_activation_pins_existing_conversations(postgres_urls):
    agents, schedulers = services(postgres_urls)
    try:
        previous = agents[1].create_conversation(PATIENT)
        agents[0].set_active_behavior(
            "v2",
            "verified-job",
            "accepted-artifact",
            Principal(role="staff", patient_id="staff-demo"),
        )
        assert agents[1].current_behavior_version() == "v2"
        assert agents[1].create_conversation(PATIENT).behavior_version == "v2"
        assert (
            agents[1].get_conversation(previous.conversation_id, PATIENT).behavior_version == "v1"
        )
        events = agents[1].store.inspect_world()["events"]
        assert any(event["kind"] == "behavior_activated" for event in events)
        with pytest.raises(PermissionError):
            agents[1].set_active_behavior("v1", "unreviewed", "unreviewed", PATIENT)
    finally:
        close_all(agents, schedulers)


def test_postgres_feedback_and_turn_share_conversation_lock(postgres_urls):
    agents, schedulers = services(postgres_urls)
    try:
        response = agents[0].create_conversation(PATIENT)
        barrier = threading.Barrier(2)

        def turn():
            barrier.wait(timeout=5)
            return agents[0].message(
                response.conversation_id,
                "request",
                "I need primary care tomorrow after 3 pm",
                PATIENT,
            )

        def feedback():
            barrier.wait(timeout=5)
            return agents[1].record_feedback(response.conversation_id, PATIENT, 5, "Helpful")

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(turn), pool.submit(feedback)]
            for result in pending:
                result.result(timeout=15)
        state = agents[1].get_conversation(response.conversation_id, PATIENT)
        assert state.status == "offering"
        assert state.options and state.request_revision == 1
        assert len(agents[1].store.inspect_world()["feedback"]) == 1
    finally:
        close_all(agents, schedulers)


def test_postgres_roles_cannot_open_another_service_database(postgres_urls):
    from urllib.parse import urlsplit, urlunsplit

    import psycopg

    source = urlsplit(postgres_urls["conversation"])
    target = urlsplit(postgres_urls["scheduling"])
    forbidden = urlunsplit((source.scheme, source.netloc, target.path, "", ""))
    try:
        connection = psycopg.connect(forbidden, connect_timeout=5)
    except psycopg.OperationalError:
        denied = True
    else:
        denied = False
        connection.close()
    assert denied, "Conversation credentials must not open the scheduling database."
