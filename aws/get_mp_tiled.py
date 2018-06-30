import os, shutil,re,sys,subprocess
import numpy as np
import pandas as pd

import threading
import queue
import subprocess
import concurrent.futures as cf

true_start_row = 0
true_start_col = 0
tiles_to_get = set()
a_needed = set()
b_needed = set()

def try_cmd(cmd, err):
    p = subprocess.Popen(cmd.split())
    out, errors = p.communicate()
    for i in range(0,3):
        if p.returncode is not 0:
            if i is 2:
                print("retry attempts exceeded! command was " + cmd)
                exit(1)
            else:
                print("will retry.")
        else:
            break


def merge(info,tile_height,tile_width,self_join):

    start_row = int(info[1]) * tile_height
    start_col = int(info[2]) * tile_width
    f = 'result_'+str(info[1])+'_'+str(info[2])
    if f+'.tar.xz' in a_needed:
        mp = pd.read_csv(f + '/mpA', header=None).values[:,0]
        mpi = pd.read_csv(f + '/mpiA', header=None).values[:,0]
        start = start_col - true_start_col
        c = matrix_profile[start:start+len(mp)] > mp
        comp = np.nonzero(c)[0]
        matrix_profile_index[comp + start] = mpi[comp] + start_row - true_start_row
        matrix_profile[comp+start] = mp[comp]
    
    if info[1] != info[2] and self_join and f+'.tar.xz' in b_needed:
        mp = pd.read_csv(f + '/B_mp', header=None).values[:,0]
        mpi = pd.read_csv(f + '/B_mpi', header=None).values[:,0]
        start = start_row - true_start_row
        c = matrix_profile[start:start+len(mp)] > mp
        comp = np.nonzero(c)[0]
        matrix_profile_index[comp + start] = mpi[comp] + start_col - true_start_col
        matrix_profile[comp+start] = mp[comp]
    
    shutil.rmtree(f)

def write_result_s3(out_s3_path):
    cmd='zip mp.zip full_matrix_profile.txt'
    try_cmd(cmd, "ERROR: Could not zip file")

    cmd='zip mpi.zip full_matrix_profile_index.txt'
    try_cmd(cmd, "ERROR: could not zip file")

    cmd = 'aws s3 cp mp.zip s3://' + out_s3_path + 'mp.zip'
    try_cmd(cmd, "ERROR: copy to s3 failed")


    cmd = 'aws s3 cp mpi.zip s3://' + out_s3_path + 'mpi.zip'
    try_cmd(cmd, "ERROR: copy to s3 failed")


# Runs in worker processes.
def producer(i):
    p = subprocess.Popen(i[3].split(), stdout=subprocess.PIPE)
    out,err = p.communicate()
    if p.returncode is not 0:
        print("Copy from s3 failed")
        print(out)
        print(err)
        print(p.returncode)
        exit(1)


    f = 'result_'+str(i[0])
    fzip = f+'.tar.xz'
    cmd = 'pxz --decompress ' + fzip
    try_cmd(cmd, "Could not unzip file") 
    cmd = 'tar xvf ' + f + '.tar'
    try_cmd(cmd, "Could not untar file") 
    os.remove(f+'.tar')
    return i

def consumer(q,tile_height,tile_width,self_join):
    while True:
        f = q.get()
        if f is None:
            break;
        else:
            merge(f.result(),tile_height,tile_width,self_join)
NUM_CPUS = 8
NUM_THREADS = 1
MAX_QUEUE_SIZE = 15
if len(sys.argv) < 7:
    print("usage: s3_bucket s3_directory tile_width tile_height matrix_profile_length self_join_flag [List of partial tiles to merge]")
    exit(1)

bucket = sys.argv[1]
directory = sys.argv[2]
tile_width = int(sys.argv[3])
tile_height = int(sys.argv[4])
matrix_profile_length = int(sys.argv[5])
self_join = bool(int(sys.argv[6]))
write_s3 = False
remove_s3_input = False
true_mp_length = matrix_profile_length
if len(sys.argv) > 7:
    min_row = 99999
    min_col = 99999
    max_col = 0
    for f in sys.argv[7:]:
        m = re.search('^\w+_(\d+)_(\d+)',f)
        if self_join and int(m.group(2)) < int(m.group(1)):
            min_row = min(int(m.group(1)), min_row)
            f_flip = 'result_'+m.group(2)+'_'+m.group(1)+'.tar.xz'
            tiles_to_get.add(f_flip)
            b_needed.add(f_flip)
        else:
            min_row = min(int(m.group(1)), min_row)
            min_col = min(int(m.group(2)), min_col)
            max_col = max(int(m.group(2)), max_col)
            tiles_to_get.add(f+'.tar.xz')
            a_needed.add(f+'.tar.xz')
    if min_row is 99999:
        min_row = 0
    if min_col is 99999:
        min_col = 0
    true_start_row = min_row * tile_height
    true_start_col = min_col * tile_width
    true_mp_length = (max_col + 1) * tile_width - true_start_col

cmd = 'aws s3 ls --recursive s3://'+bucket+'/'+directory
process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
output, error = process.communicate()
matrix_profile = np.zeros(true_mp_length,dtype=np.float32)
matrix_profile.fill(float('inf'));
matrix_profile_index = np.zeros(true_mp_length,dtype=np.uint32)

#Take only the file paths from s3 ls
files = output.split()[3::4]
num_files = len(files)

if num_files == 0:
    print("No s3 files found")
    exit(0)

copy_commands = []

for i, line in enumerate(files):
    x = str(line, 'utf-8').split('/')[1]
    m = re.search('^\w+_(\d+)_(\d+)\.tar.xz',x)
    info = (i, m.group(1), m.group(2), 'aws s3 cp s3://'+bucket+'/'+directory+'/'+x+' result_'+str(i)+'.tar.xz')
    if x in tiles_to_get:
        copy_commands.append(info)

sumlock = threading.Lock()
result_queue = queue.Queue(MAX_QUEUE_SIZE)
total = 0
NUM_TO_DO = len(copy_commands)

with cf.ThreadPoolExecutor(NUM_THREADS) as tp:
    # start the threads running `merge`
    for _ in range(NUM_THREADS):
        tp.submit(consumer, result_queue, tile_height, tile_width, self_join)
    # start the worker processes
    with cf.ProcessPoolExecutor(NUM_CPUS) as pp:
        for i in copy_commands:
            # blocks until the queue size <= MAX_QUEUE_SIZE
            f = pp.submit(producer,i)
            result_queue.put(f)
    # tell threads we're done
    for _ in range(NUM_THREADS):
        result_queue.put(None)

mp = open('full_matrix_profile.txt', "w")
mpi = open('full_matrix_profile_index.txt', "w")

for number, idx in zip(matrix_profile, matrix_profile_index):
    mp.write(str(number) + '\n')
    mpi.write(str(idx) + '\n')

mp.close()
mpi.close()

if write_s3:
    write_result_s3(out_s3_path) 

print("Finished!")

