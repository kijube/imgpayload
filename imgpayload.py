#!/usr/bin/python3
import random
import subprocess
import threading
import time
from argparse import ArgumentParser
from os import mkdir
from os.path import join, basename
from queue import Queue
from shlex import split
from shutil import copy

from rich import print
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

temp_dir = "tmp"
results_dir = "results"

img_placeholder = "{in}"
out_img_placeholder = "{out}"

min_block_size = 3


def parse_arguments():
    parser = ArgumentParser()
    parser.add_argument("--payload", type=str, required=True, help="The payload to inject")
    parser.add_argument("images", nargs="+", type=str, help="The image files to test")
    parser.add_argument("--beat", type=str, required=False, metavar="BEAT_CMD",
                        help="Shell command that the injected payload should survive")
    parser.add_argument("--shell", type=str, required=False, default="sh",
                        help="The shell to run the beat command with")
    parser.add_argument("--threads", type=int, required=False, default=2, help="The amount of threads to use")

    return parser.parse_args()


def cleanup_dirs():
    rm_file(temp_dir)
    mkdir(temp_dir)
    rm_file(results_dir)
    mkdir(results_dir)


def main():
    print(_HEADER)
    args = parse_arguments()

    source_image_files = args.images
    payload = args.payload
    beat_cmd = args.beat
    thread_count = args.threads
    shell = args.shell

    cleanup_dirs()

    payloader = ImgPayloader(source_image_files, payload, beat_cmd, thread_count, shell)
    payloader.run()


class ImgPayloader:
    def __init__(self, source_imgs, payload, beat_cmd: str, thread_count, shell):
        self.source_imgs = source_imgs
        self.shell = shell
        self.payload = payload
        self.beat_cmd = beat_cmd
        self.imgs = []
        self.threads = []
        self.global_task = None
        self.thread_count = min(len(self.source_imgs), thread_count)

    # Copy image files that should be injected to temp directory
    def copy_source_images(self):
        for image_file in self.source_imgs:
            new_path = join(temp_dir, basename(image_file))
            self.imgs.append(new_path)
            copy(image_file, new_path)

    def run_beat_cmd(self, file, out_file):
        # replace placeholders with path and split cmd string to args with shlex
        cmd_args = split(
            self.shell + " " + self.beat_cmd.replace(img_placeholder, file).replace(out_img_placeholder, out_file))
        proc = subprocess.Popen(cmd_args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, errs = proc.communicate(timeout=10)  # todo make changeable
            return True, out, errs
        except TimeoutError:
            out, errs = proc.communicate()
            return False, out, errs

    # Run image payloader
    def run(self):
        cleanup_dirs()
        self.copy_source_images()
        self.print_start_info()

        process_queue = list(self.imgs)

        with Progress() as progress:
            # total progress task (how many images to go)
            self.global_task = progress.add_task("[green]\\[total progress][/green]", total=len(self.imgs))
            while len(self.threads) > 0 or len(process_queue) > 0:
                if len(self.threads) < self.thread_count and len(process_queue) > 0:
                    image_file = process_queue.pop()
                    processor = ImageProcessorThread(image_file, progress, self)
                    task = progress.add_task(processor.prefix + "...", total=processor.calculate_total())
                    processor.task = task
                    self.threads.append(processor)
                    processor.start()

                # process thread messages
                _threads = list(self.threads)
                for thread in _threads:
                    while not thread.messages.empty():
                        msg = thread.messages.get()
                        msg_type = msg[0]
                        msg_val = msg[1]
                        if msg_type == "log":
                            progress.console.print(msg_val)
                        elif msg_type == "advance":
                            progress.advance(thread.task, int(msg_val))
                        elif msg_type == "status":
                            if msg_val == "done" and not thread.running:
                                progress.remove_task(thread.task)
                                thread.finish()
                                self.threads.remove(thread)
            progress.remove_task(self.global_task)
        print("YES ALL IS DONE")

    def print_start_info(self):
        print(f"* Payload: [bold]{self.payload}[/bold]")
        print(f"* Command to beat: [bold]{self.beat_cmd}[/bold]")
        img_files_string = ", ".join([basename(f) for f in self.imgs])
        if len(img_files_string) > 32:
            img_files_string = img_files_string[:32] + "..."
        print(f"* Images ({len(self.imgs)}): [bold]{img_files_string}[/bold]")
        print(f"* Threads: {self.thread_count}")
        print("\n")


def read(file_name):
    with open(file_name, "rb") as f:
        return f.read()


class WorkerThread(threading.Thread):
    def __init__(self, payloader: ImgPayloader, img_file):
        super().__init__()
        self.payloader = payloader
        self.img_file = img_file

    def run(self):
        pass


class ImageProcessorThread(threading.Thread):
    def __init__(self, img_file, progress: Progress, payloader: ImgPayloader):
        super().__init__()
        self.img = img_file
        self.progress = progress
        self.name = basename(self.img)
        self.prefix = f"[blue]{self.name}[/blue]"
        self.payloader = payloader
        self.messages = Queue()
        self.running = False
        self.copy_counter = 0
        self.block_matches = None
        self.dir = join(temp_dir, f"_{self.name}_")

    def run(self):
        self.log("Preparing...")
        self.prepare()
        self.log(f"Scanning blocks...")
        self.compare_blocks()
        while not self.running:
            time.sleep(0.1)
            self.advance(1)
            if random.randrange(0, 50) == 10:
                self.done()

    def finish(self):
        self.running = True

    def to_local_file(self, file):
        return join(self.dir, file)

    def create_tmp_copy(self):
        self.copy_counter += 1
        file_name = self.to_local_file(self.name + hex(self.copy_counter)[2:])
        with open(self.img, "rb") as fi:
            with open(file_name, "wb") as fo:
                fo.write(fi.read())
        return file_name

    def done(self):
        self.messages.put(("status", "done"))

    def log(self, *msg):
        self.messages.put(("log", "".join([f"{self.prefix}: ", str(*msg)])))

    def advance(self, amount):
        self.messages.put(("advance", amount))

    def calculate_total(self):
        return 100

    def prepare(self):
        mkdir(self.dir)

    def compare_blocks(self):
        original = self.create_tmp_copy()
        to_process = self.create_tmp_copy()
        processed = to_process + "_prcd"
        success = self.payloader.run_beat_cmd(to_process, processed)
        if not success:
            self.log("[red]Something went wrong when executing the beat cmd while comparing blocks.[/red]")
            return

        original_data = read(original)
        processed_data = read(processed)

        block_matches = []
        block_match = None
        for i in range(0, min(len(original_data), len(processed_data))):
            if original_data[i] != processed_data[i]:
                if block_match is not None:
                    if block_match.size >= min_block_size:
                        block_matches.append(block_match)
                    block_match = None
                continue

            if block_match is None:
                block_match = BlockMatch(i)

            block_match.size += 1

        if block_match is not None and block_match.size >= min_block_size:
            block_matches.append(block_match)

        self.block_matches = list(block_matches)
        list.sort(self.block_matches, key=lambda x: -x.size)

        # todo if no block matches found return immediately
        # todo check if payload fits inside?

        self.log(f"Found {len(self.block_matches)} block matches, trying to fit payload...")


class BlockMatch:
    def __init__(self, index):
        self.index = index
        self.size = 0

    def __str__(self):
        return f"{self.size} @ {self.index}"


if __name__ == "__main__":
    main()
