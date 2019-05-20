from PySide2.QtCore import *
import numpy as np
from fabric import Connection
import os, io
import time
import pickle
from fabric import Connection
import tempfile
import errno

from settings import cluster_address, cluster_path

local_path = os.environ['PWD']
cluster = Connection(cluster_address)


def drag_file():
    return 'drag_cache.npy'


def load_drag():
    try:
        drag = np.load(drag_file())
    except FileNotFoundError:
        drag = np.empty((0,2))

    return drag


def save_drag(drag):
    np.save(drag_file(),drag)


def all_available_indices_and_names():
    dir = 'simulations'
    simulations = []

    for file in os.listdir(dir):
        if 'run' in file:
            try:
                index = int(file.replace('run', ''))
                name = load_simulation_name(index)
                simulations.append([index, name])
            except Exception as e:
                print(f'failed to load file: {file}')
                print(str(e))

    return simulations


def run_directory(index):
    directory = 'simulations/run{index}'.format(index=index)

    while not os.path.exists(directory):
        try:
            os.makedirs(directory)
        except OSError as e:
            print(f'directory creation failed: {directory}')

    return directory


def run_filepath(index, filename):
    directory = run_directory(index)
    path = os.path.join(directory, filename)
    return path


def get_run_completion_percentage(index):
    ''' Read the completion percentage of the run
    '''
    try:
        directory = '{}/outbox/run{}'.format(cluster_path, index)
        grep = "grep 'MAIN:  Time:' output"
        get_last = " | tail -n 1"
        get_time = " | awk '{print $3}'"
        command = grep + get_last + get_time

        with cluster.cd(directory):
            output = cluster.run(command, hide=True).stdout

        numbers = output.split('/')
        percentage = int(100 * float(numbers[0]) / float(numbers[1]))
    except:
        # Most likely file not found
        percentage = 0
    return percentage


def write_outline(filename, outline):
    flipped_outline = np.copy(outline.reshape((-1, 2)))
    flipped_outline[:, 1:] = 480 - flipped_outline[:, 1:]
    np.savetxt(filename, flipped_outline, fmt='%i %i')


def setup_cluster_inbox():
    try:
        cluster.sftp().stat(cluster_path+'/inbox')
    except IOError:
        cluster.sftp().mkdir(cluster_path+'/inbox')
        cluster.sftp().mkdir(cluster_path+'/signal_in')

def queue_run(contour, index):
    # save contour to file and copy to the cluster inbox
    filename = run_filepath(index, "contour.dat")
    write_outline(filename, contour)

    setup_cluster_inbox()    
    
    # copy the contour
    remote_name = '{}/inbox/run{}'.format(cluster_path, index)
    cluster.put(filename, remote=remote_name)

    # copy a signal file across
    remote_name = '{}/signal_in/run{}'.format(cluster_path, index)
    cluster.sftp().file(remote_name, 'a').close()




def save_simulation(simulation):

    name = simulation['name']
    index = simulation['index']

    # save name
    with open(run_filepath(index, 'name.npy'), 'wb') as file:
        pickle.dump(name, file, protocol=pickle.HIGHEST_PROTOCOL)

    # save simulation
    with open(run_filepath(index, 'simulation.npy'), 'wb') as file:
        pickle.dump(simulation, file, protocol=pickle.HIGHEST_PROTOCOL)


def load_simulation_name(index):
    # save simulation
    with open(run_filepath(index, 'name.npy'), 'rb') as file:
        return pickle.load(file)


def load_simulation(index):
    # save simulation
    with open(run_filepath(index, 'simulation.npy'), 'rb') as file:
        return pickle.load(file)


def fetch_activity():
    with cluster.cd(cluster_path):
        output = cluster.run('''bash cpuloadinfo.sh''', hide=True).stdout
    cpu_usage = output.split('\n')[1:-1]
    cpu_usage = [
        float(cpu_usage_meas.split(' ')[1]) for cpu_usage_meas in cpu_usage
    ]
    cpu_usage = np.array(cpu_usage)
    return cpu_usage


if __name__ == '__main__':
    all_available_indices_and_names()


# Function for working with incoming signals
def get_signals():
    path = cluster_path+"/signal_out/"
    try:
        dirlist = cluster.sftp().listdir(path)
        return set(dirlist)
    except FileNotFoundError:
        print('not found')
        cluster.sftp().mkdir(path)
        return set([])


existing_signals = get_signals()


def create_incoming_signal( index, signal_type, slot):
    ''' create an incoming signal, only useful for testing '''
    filename = 'signal_out/run{}_{}_{}'.format(index, signal_type, slot)
    path = cluster_path+'/'+filename
    cluster.sftp().open(path, 'a').close()


def remove_incoming_signal(signal):
    ''' remove a signal, only useful for testing '''
    global existing_signals
    path = cluster_path+'/signal_out/'+signal
    if os.path.exists(path):
        os.remove(path)
    existing_signals = existing_signals - set(signal)
    

def get_new_signals():
    global existing_signals
    signals = get_signals()
    return signals - existing_signals


def add_new_signal(signal):
    global existing_signals
    existing_signals.add(signal)


def get_signal_info(signal):
    run, signal_type, slot = signal.split('_')
    slot = int(slot)
    index = run.replace("run", '')
    index = int(index)
    return index, signal_type, slot


def download_results(index):
    localfolder = run_directory(index)
    remotefolder = 'outbox/run{}'.format(index)
    remotepath = cluster_path+'/'+remotefolder
    for filename in cluster.sftp().listdir(remotepath):
        cluster.sftp().get(
            remotepath+'/'+filename,
            os.path.join(localfolder,filename)
        )


def queue_running():
    setup_cluster_inbox()
    remote_name = '{}/signal_in/ping'.format(cluster_path)
    cluster.sftp().file(remote_name, 'a').close()

    remote_name = '{}/signal_out/pong'.format(cluster_path)
    for i in range(5):
        try:
            cluster.sftp().remove(remote_name)
            return True
        except IOError:
            time.sleep(1)

    return False


class RunCompleteWatcher(QThread):
    ''' Periodically polls the cluster to check for finished jobs
        Gets the resulting images as numpy arrays and 
        communicates them through a signal
    '''

    started = Signal(object)
    completed = Signal(int)

    def run(self):
        while True:
            for signal in get_new_signals():
                add_new_signal(signal)
                index, signal_type, slot = get_signal_info(signal)

                print("{} signal for run {} in slot {}!".format(
                    signal_type, index, slot))

                if signal_type == "start":
                    self.started.emit((index, slot))
                elif signal_type == "end":
                    download_results(index)
                    self.completed.emit(index)

            time.sleep(2)



def test_app():
    app = QApplication(sys.argv)
    label = QLabel("<font color=red size=40>Hello World!</font>")
    label.show()
    rcw = RunCompleteWatcher()
    sys.exit(app.exec_())


