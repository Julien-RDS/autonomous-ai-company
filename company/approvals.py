"""Persist human approval atomically; replay is safe across checkpoint commits."""
from sqlalchemy import select, text
from .database import Task, Approval, AuditEvent


def approve_task(sessions, task_id, actor='local-cli', checkpointer=None):
    if not actor.strip():
        raise ValueError('Identité humaine requise')
    def record(state):
        with sessions.begin() as db:
            if db.get_bind().dialect.name=='sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            task=db.scalar(select(Task).where(Task.id==task_id).with_for_update())
            existing=db.scalar(select(Approval).where(Approval.task_id==task_id))
            if existing:
                return {'approved': True}
            if task is None or task.status not in ('pending_review', 'approved'):
                raise ValueError('Tâche inconnue ou déjà traitée')
            task.status='approved'
            db.add(Approval(task_id=task_id,actor=actor))
            db.add(AuditEvent(entity_id=task_id,kind='human_approved'))
        return {'approved': True}
    if checkpointer is None:
        return record({})
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt, Command
    from typing import TypedDict
    class ApprovalState(TypedDict, total=False):
        task_id: str
        approved: bool
    def confirmation(state):
        if interrupt({'task_id': task_id, 'requires_human': True}) is not True:
            raise ValueError('Approbation explicite requise')
        return record(state)
    builder=StateGraph(ApprovalState)
    builder.add_node('human_approval',confirmation)
    builder.add_edge(START,'human_approval'); builder.add_edge('human_approval',END)
    graph=builder.compile(checkpointer=checkpointer)
    config={'configurable':{'thread_id':f'approval:{task_id}'}}
    snapshot=graph.get_state(config)
    if not snapshot.values:
        graph.invoke({'task_id':task_id},config)
    elif not snapshot.next:
        return snapshot.values
    # This function is called only by the explicit CLI approve command.
    return graph.invoke(Command(resume=True),config)
