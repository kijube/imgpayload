#!/usr/bin/python3
import random
import subprocess
import threading
import time
from argparse import ArgumentParser
from os import mkdir
from os.path import join, basename, splitext
from queue import Queue, Empty
from shlex import split
from shutil import copy

from rich import print
from rich.markup import escape
from rich.progress import Progress

from utils import *

_HEADER = """[red]
   _                           _           _ 
  |_|_____ ___ ___ ___ ___ _ _| |___ ___ _| |
  | |     | . |___| . | .'| | | | . | .'| . |
  |_|_|_|_|_  |   |  _|__,|_  |_|___|__,|___|
          |___|   |_|     |___|              [/red]
      Inject payloads into image files.
 """

TEMP_DIR = "tmp"
RESULTS_DIR = "results"

IN_IMG_PLACEHOLDER = "{in}"
OUT_IMG_PLACEHOLDER = "{out}"

MIN_BLOCK_SIZE = 2

WORKER_COOLDOWN = 0

MSG_TYPE_STATUS = "status"
MSG_TYPE_RESULT = "result"
MSG_TYPE_LOG = "log"
MSG_STATUS_DONE = "done"

JOB_TYPE_BLOCK_MATCHES = "job_block_matches"
JOB_TYPE_CHECK_IMAGE = "job_check_image"
JOB_TYPE_INJECT_PAYLOAD = "job_inject_payload"

JOB_RESULT_CHECK_IMAGE_FAILED = "check_image_failed"
JOB_RESULT_CHECK_IMAGE_SUCCEEDED = "check_image_succeeded"


def parse_arguments():
    parser = ArgumentParser()
    parser.add_argument("--payload", type=str, required=True, help="The payload to inject")
    parser.add_argument("images", nargs="+", type=str, help="The image files to test")
    parser.add_argument("--beat", type=str, required=True, metavar="BEAT_CMD",
                        help="Shell command that the injected payload should survive")
    parser.add_argument("--split-by", type=str, required=False, metavar="SPLIT_BY",
                        help="The character by which the payload can be split into "
                             "multiple parts to fit it into smaller blocks if necessary")
    parser.add_argument("--shell", type=str, required=False, default="sh",
                        help="The shell to run the beat command with")
    parser.add_argument("--threads", type=int, required=False, default=2, help="The amount of threads to use")

    return parser.parse_args()


def cleanup_dirs():
    rm_file(TEMP_DIR)
    mkdir(TEMP_DIR)
    rm_file(RESULTS_DIR)
    mkdir(RESULTS_DIR)


def main():
    print(_HEADER)
    args = parse_arguments()

    source_image_files = args.images
    payload = args.payload
    beat_cmd = args.beat
    thread_count = args.threads
    shell = args.shell
    split_by = args.split_by

    cleanup_dirs()

    payloader = ImgPayloader(source_image_files, payload, beat_cmd, thread_count, shell, split_by)
    payloader.run()


class ImgPayloader:
    def __init__(self, source_imgs, payload, beat_cmd: str, thread_count, shell, split_by):
        self.source_imgs = source_imgs
        self.shell = shell
        self.payload = payload
        self.beat_cmd = beat_cmd
        self.imgs = []
        self.global_task = None
        self.split_by = split_by
        self.thread_count = max(1, thread_count)
        self.img_contexts: list[ImageContext] = []
        self.workers: list[WorkerThread] = []
        self.job_queue = Queue()

    # Copy image files that should be injected to temp directory
    def copy_source_images(self):
        for image_file in self.source_imgs:
            new_path = join(TEMP_DIR, basename(image_file))
            self.imgs.append(new_path)
            copy(image_file, new_path)

    def run_beat_cmd(self, file, out_file):
        # replace placeholders with path and split cmd string to args with shlex
        cmd_args = split(
            self.shell + " " + self.beat_cmd.replace(IN_IMG_PLACEHOLDER, file).replace(OUT_IMG_PLACEHOLDER, out_file))
        proc = subprocess.Popen(cmd_args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # noinspection PyBroadException
        try:
            out, errs = proc.communicate(timeout=10)  # todo make changeable
            # rm_file(file)
            return True, out, errs
        except:
            out, errs = proc.communicate()
            return False, out, errs

    def is_finished(self):
        if not self.job_queue.empty():
            return False

        for ctx in self.img_contexts:
            job = ctx.get_new_job(modify=False)
            if job is not None:
                return False

        for worker in self.workers:
            if worker.is_working:
                return False

        return True

    def create_contexts(self):
        for img in self.imgs:
            self.img_contexts.append(ImageContext(img, self))

    def start_workers(self):
        for i in range(0, self.thread_count):
            self.workers.append(WorkerThread(self, i))

        for worker in self.workers:
            worker.start()

    # Run image payloader
    def run(self):
        cleanup_dirs()
        self.copy_source_images()
        self.print_start_info()
        self.create_contexts()
        self.start_workers()

        total = 0

        print("")
        with Progress() as progress:
            self.global_task = progress.add_task("[green]\\[total progress][/green]", total=len(self.imgs))

            for img_ctx in self.img_contexts:
                task = progress.add_task(img_ctx.prefix + "...")
                img_ctx.task = task

            while not self.is_finished():
                # check for new work
                for img_ctx in self.img_contexts:
                    job = img_ctx.get_new_job()

                    if job is not None:
                        img_ctx.update_remaining_jobs(1, progress)
                        total += 1
                        progress.update(self.global_task, total=total)
                        self.job_queue.put(job)

                # process thread messages
                for worker in self.workers:
                    while not worker.messages.empty():
                        msg = worker.messages.get()
                        msg_type = msg[0]
                        msg_val = msg[1]

                        if msg_type == MSG_TYPE_LOG:
                            print(msg_val)
                        elif msg_type == MSG_TYPE_RESULT:
                            job_result = msg_val
                            worker.job_info.img_context.receive_result(job_result)
                        elif msg_type == MSG_TYPE_STATUS:
                            if msg_val == MSG_STATUS_DONE:
                                worker.job_info.img_context.update_remaining_jobs(-1, progress)
                                progress.advance(self.global_task, 1)
        print("YES ALL IS DONE")

    def print_start_info(self):
        print(f"* Payload: [bold]{escape(self.payload)}[/bold]")
        print(f"* Command to beat: [bold]{escape(self.beat_cmd)}[/bold]")
        img_files_string = ", ".join([basename(f) for f in self.imgs])
        if len(img_files_string) > 32:
            img_files_string = img_files_string[:32] + "..."
        print(f"* Images ({len(self.imgs)}): [bold]{img_files_string}[/bold]")
        print(f"* Threads: {self.thread_count}")
        print("")


def read(file_name):
    with open(file_name, "rb") as f:
        return f.read()


class ImageContext:
    def __init__(self, img, payloader: ImgPayloader):
        self.img = img
        self.payloader = payloader
        self.name = basename(self.img)
        self.prefix = f"[blue]{self.name}[/blue]"
        self.dir = join(TEMP_DIR, f"_{self.name}_")
        self.copy_counter = 0
        self.task = -1
        self.remaining_jobs = 0
        self.total_jobs = 0
        self.jobs = Queue()

        self.block_matches: list[BlockMatch] = None

        self.add_start_jobs()

        if not exists(self.dir):
            mkdir(self.dir)

    def create_tmp_copy(self, info=""):
        self.copy_counter += 1
        file_name = self.to_local_file(self.name + "_" + hex(self.copy_counter)[2:]) + info
        with open(self.img, "rb") as fi:
            with open(file_name, "wb") as fo:
                fo.write(fi.read())
        return file_name

    def to_local_file(self, file):
        return join(self.dir, file)

    def get_new_job(self, modify=True):
        if not self.jobs.empty():
            if not modify:
                return "still has jobs"
            return self.jobs.get()
        return None

    def add_start_jobs(self):
        self.jobs.put(JobInfo(self, JOB_TYPE_BLOCK_MATCHES, None))

    def receive_result(self, job_result):
        job_type = job_result.job_info.job_type
        if job_type == JOB_TYPE_BLOCK_MATCHES:
            self.block_matches = job_result.result
            self.try_inject_payload()
        elif job_type == JOB_TYPE_INJECT_PAYLOAD:
            self.add_check_job(job_result)
        elif job_type == JOB_TYPE_CHECK_IMAGE:
            if job_result.result[0] == JOB_RESULT_CHECK_IMAGE_SUCCEEDED:
                self.handle_successful_injection(job_result)

    def try_inject_payload(self):
        if self.payloader.split_by is not None:
            permutations = self.find_split_permutations()
            print(f"{self.prefix}: Found {len(permutations)} split permutations for the payload")
            for perm in permutations:
                self.jobs.put(JobInfo(self, JOB_TYPE_INJECT_PAYLOAD, ("split", perm)))

        for match in self.block_matches:
            self.jobs.put(JobInfo(self, JOB_TYPE_INJECT_PAYLOAD, ("default", match)))

    def add_check_job(self, job_result):
        img_file = job_result.result
        self.jobs.put(JobInfo(self, JOB_TYPE_CHECK_IMAGE, (img_file, job_result.job_info.job_args[0])))

    def handle_successful_injection(self, job_result):
        with open(job_result.result[1], "rb") as fi:
            _, ext = splitext(self.name)
            path = join(RESULTS_DIR, basename(job_result.result[1]) + "_success" + ext)
            with open(path, "wb") as fo:
                fo.write(fi.read())
        print(
            f"[bold green]Success! [/bold green][green]Payload was injected into image {self.name} (written to: {path})[/green]")

    def find_split_permutations(self):
        parts = self.payloader.payload.split(self.payloader.split_by)
        permutations = []
        for block_start in range(0, len(self.block_matches)):
            curr_perm = []
            part_idx = 0
            for curr_block_idx in range(block_start, len(self.block_matches)):
                if len(curr_perm) == len(parts):
                    continue
                part = parts[part_idx]
                curr_block = self.block_matches[curr_block_idx]
                if curr_block.size >= len(part):
                    curr_perm.append(curr_block_idx)
                    part_idx += 1

            if len(curr_perm) == len(parts):
                permutations.append(curr_perm)

        return permutations

    def update_remaining_jobs(self, delta, progress):
        self.remaining_jobs += delta
        if delta > 0:
            self.total_jobs += delta
            delta = 0
        delta = abs(delta)
        progress.update(self.task, description=self.prefix + "(" + str(self.remaining_jobs) + ")",
                        total=self.total_jobs, advance=delta)


class JobInfo:
    def __init__(self, img_context: ImageContext, job_type, job_data):
        self.img_context = img_context
        self.job_type = job_type
        self.job_args = job_data

    def __str__(self):
        return f"Job '{self.job_type}' for image '{self.img_context.name}' with args '{str(self.job_args)[:100]}'"


class WorkerContext:
    def __init__(self, job_info: JobInfo):
        self.job_info = job_info


class WorkerThread(threading.Thread):
    def __init__(self, payloader: ImgPayloader, worker_id):
        super().__init__()
        self.worker_id = worker_id
        self.payloader = payloader
        self.job_info: JobInfo = None
        self.messages = Queue()
        self.running = True
        self.is_working = False
        self.cooldown = 0
        self.job_handlers = {
            JOB_TYPE_BLOCK_MATCHES: self.compare_blocks,
            JOB_TYPE_INJECT_PAYLOAD: self.inject_payload,
            JOB_TYPE_CHECK_IMAGE: self.check_image
        }

    def send_message(self, type_: str, value):
        self.messages.put((type_, value))

    def get_job(self):
        try:
            self.job_info = self.payloader.job_queue.get(block=True, timeout=1)
            return True
        except Empty:
            return False

    def work(self):
        self.cooldown = WORKER_COOLDOWN
        # self.log("got new job " + str(self.job_info))
        handler = self.job_handlers[self.job_info.job_type]
        handler(self.job_info, self.job_info.job_args)

    def send_result(self, result):
        self.send_message(MSG_TYPE_RESULT, JobResult(self, self.job_info, result))

    def done(self):
        self.send_message(MSG_TYPE_STATUS, MSG_STATUS_DONE)

    def log(self, *msg):
        self.send_message(MSG_TYPE_LOG, "".join([f"{self.get_prefix()}: ", str(*msg)]))

    def get_prefix(self):
        prefix = f"[green]t{self.worker_id}[/green]-"
        if self.job_info is not None:
            prefix += self.job_info.img_context.prefix
        return prefix

    def run(self):
        while self.running:
            time.sleep(0.25)
            if self.get_job():
                self.is_working = True
                self.work()
                self.done()
            else:
                self.cooldown -= 1
                if self.cooldown < 0:
                    self.is_working = False

    def inject_payload(self, job_info: JobInfo, args):
        img = None
        edited_data = b""

        if args[0] == "default":
            img = self.job_info.img_context.create_tmp_copy("_def")
            data = read(img)
            match: BlockMatch = args[1]
            payload = bytes(self.payloader.payload, "ascii")  # todo make encoding changeable?
            edited_data += bytes(data[:match.index])
            edited_data += bytes(payload)
            edited_data += bytes(data[match.index + len(payload):])

        elif args[0] == "split":
            img = self.job_info.img_context.create_tmp_copy("_splt")
            data = read(img)
            permutation = args[1]
            payload = [bytes(x, "ascii") for x in
                       self.payloader.payload.split(self.payloader.split_by)]  # todo make encoding changeable?
            curr_idx = 0
            payload_idx = 0
            for perm_match in permutation:
                match = self.job_info.img_context.block_matches[perm_match]

                edited_data += bytes(data[curr_idx:match.index])

                payload_val = payload[payload_idx]
                edited_data += bytes(payload_val)

                if len(payload_val) != match.size:
                    edited_data += bytes(data[match.index + len(payload_val):match.index + match.size])
                curr_idx = match.index + match.size
                payload_idx += 1

            edited_data += data[curr_idx:]

        with open(img, "wb") as f:
            f.write(edited_data)

        self.send_result(img)

    def check_image(self, job_info: JobInfo, args):
        img_file = args[0]
        mode = args[1]
        result_file = img_file + "_prcd"
        success, out, err = self.payloader.run_beat_cmd(img_file, result_file)
        if not exists(result_file):
            if len(err) > 0: # todo maybe add print error switch?
                self.log(f"[red]Got an error when checking image {self.job_info.img_context.name}:[/red]")
                self.log(err)
            self.send_result((JOB_RESULT_CHECK_IMAGE_FAILED,))
            return
        data = read(result_file)
        if mode == "default":
            if bytes(self.payloader.payload, "ascii") in data:  # todo make encoding changeable?
                self.send_result((JOB_RESULT_CHECK_IMAGE_SUCCEEDED, img_file))
        elif mode == "split":
            last_idx = -1
            for part in [bytes(x, "ascii") for x in
                         self.payloader.payload.split(self.payloader.split_by)]:  # todo encoding changeable
                try:
                    curr_idx = data.index(part)
                    if curr_idx <= last_idx:
                        self.send_result((JOB_RESULT_CHECK_IMAGE_FAILED,))
                        return
                except ValueError:
                    self.send_result((JOB_RESULT_CHECK_IMAGE_FAILED,))
                    return
            self.send_result((JOB_RESULT_CHECK_IMAGE_SUCCEEDED, img_file))
            return

    def compare_blocks(self, job_info: JobInfo, args):
        original = job_info.img_context.create_tmp_copy()
        to_process = job_info.img_context.create_tmp_copy()
        processed = to_process + "_prcd"
        success, out, err = self.payloader.run_beat_cmd(to_process, processed)
        if not success:
            self.log("[red]Something went wrong when executing the beat cmd while comparing blocks.[/red]")
            return

        original_data = read(original)

        if not exists(processed):
            self.log(f"[red]Error! Processed file does not exist for image {self.job_info.img_context.name}[/red]")
            if len(err) > 0:
                self.log("Received error output:")
                self.log(err)
            return

        processed_data = read(processed)

        block_matches = []
        block_match = None
        for i in range(0, min(len(original_data), len(processed_data))):
            if original_data[i] != processed_data[i]:
                if block_match is not None:
                    if block_match.size >= MIN_BLOCK_SIZE:
                        block_matches.append(block_match)
                    block_match = None
                continue

            if block_match is None:
                block_match = BlockMatch(i)

            block_match.size += 1

        if block_match is not None and block_match.size >= MIN_BLOCK_SIZE:
            block_matches.append(block_match)

        block_matches = list(block_matches)
        list.sort(block_matches, key=lambda x: x.index)

        # todo if no block matches found return immediately
        # todo check if payload fits inside?

        self.log(f"Found {len(block_matches)} block matches")

        self.send_result(block_matches)


class JobResult:
    def __init__(self, worker: WorkerThread, job_info: JobInfo, result):
        self.worker = worker
        self.job_info = job_info
        self.result = result


class BlockMatch:
    def __init__(self, index):
        self.index = index
        self.size = 0

    def __str__(self):
        return f"{self.size} @ {self.index}"


if __name__ == "__main__":
    main()
