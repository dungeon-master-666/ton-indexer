import time
import sys
from celery.signals import worker_ready
from indexer.celery import app
from indexer.tasks import get_block, get_last_mc_block
from indexer.database import init_database, Block, get_session
from config import settings
from loguru import logger

def wait_for_broker_connection():
    while True:
        try:
            app.broker_connection().ensure_connection(max_retries=3)
        except Exception:
            logger.warning(f"Can't connect to celery broker. Trying again...")
            time.sleep(3)
            continue
        logger.info(f"Connected to celery broker.")
        break

def dispatch_seqno_list(mc_seqno_list, queue):
    pass

def get_existing_seqnos(min_seqno, max_seqno):
    """
    Returns set of tuples of existing seqnos: {(19891542,), (19891541,), (19891540,)}
    """
    session = get_session()()
    with session.begin():
        seqnos_already_in_db = session.query(Block.seqno).filter(Block.workchain==-1).filter(Block.seqno >= min_seqno).filter(Block.seqno <= max_seqno).all()
    
    return set(seqnos_already_in_db)

def forward_main(queue):
    init_database()

    wait_for_broker_connection()

    current_seqno = settings.indexer.init_mc_seqno + 1
    while True:
        last_mc_block = get_last_mc_block.apply_async([], serializer='pickle', queue=queue).get()
        if last_mc_block['seqno'] < current_seqno:
            time.sleep(0.2)
            continue

        for seqno in range(current_seqno, last_mc_block['seqno'] + 1):
            get_block.apply_async([[seqno]], serializer='pickle', queue=queue).get()

        current_seqno = last_mc_block['seqno'] + 1

        time.sleep(0.2)
        logger.info(f"Current seqno: {current_seqno}")

def backward_main(queue):
    init_database()

    wait_for_broker_connection()

    logger.info(f"Backward scheduler started. From {settings.indexer.init_mc_seqno} to {settings.indexer.smallest_mc_seqno}.")

    seqnos_already_in_db = get_existing_seqnos(settings.indexer.smallest_mc_seqno, settings.indexer.init_mc_seqno)
    seqnos_to_process = range(settings.indexer.init_mc_seqno, settings.indexer.smallest_mc_seqno - 1, -1)
    seqnos_to_process = [seqno for seqno in seqnos_to_process if (seqno,) not in seqnos_already_in_db]
    logger.info(f"{len(seqnos_already_in_db)} seqnos already exist in DB")
    del seqnos_already_in_db

    parallel = settings.indexer.workers_count
    start_time = time.time()


    left_index = 0
    tasks_in_progress = []
    while left_index < len(seqnos_to_process):
        finished_tasks = [task for task in tasks_in_progress if task.ready()]
        for finished_task in finished_tasks:
            finished_task.get()
        tasks_in_progress = [task for task in tasks_in_progress if task not in finished_tasks]
        if len(tasks_in_progress) >= parallel:
            time.sleep(0.05)
            continue

        right_index = min(left_index + settings.indexer.blocks_per_task, len(seqnos_to_process))
        next_chunk = seqnos_to_process[left_index:right_index]

        logger.info(f"Dispatching chunk: [{left_index}, {right_index})")
        tasks_in_progress.append(get_block.apply_async([next_chunk], serializer='pickle', queue=queue))
        
        left_index = right_index

if __name__ == "__main__":
    if sys.argv[1] == 'backward':
        backward_main(sys.argv[2])
    elif sys.argv[1] == 'forward':
        forward_main(sys.argv[2])
    else:
        raise Exception("Pass direction in argument: backward/forward")
