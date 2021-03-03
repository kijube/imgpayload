from os import remove
from os.path import exists, isdir
from shutil import rmtree


def rm_file(file):
    if not exists(file):
        return

    if is_dir(file):
        rmtree(file)
    else:
        remove(file)


def is_dir(file):
    return exists(file) and isdir(file)
