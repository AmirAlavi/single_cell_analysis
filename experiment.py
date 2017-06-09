"""Retrieval Experiment Runner

Usage:
    experiment.py <email_address>
"""
# import pdb; pdb.set_trace()
from os.path import join, exists, basename, normpath, isfile, isdir
from os import makedirs, listdir
import time
from collections import defaultdict
import string
import sys
import subprocess
import csv

from docopt import docopt
import numpy as np

DEFAULT_WORKING_DIR_ROOT='experiments'
DEFAULT_MODELS_FILE='experiment_models.list'
REDUCE_COMMAND_TEMPLATE="""python scrna.py reduce {trained_nn_folder} \
--data=data/integrate_imputing_dataset_kNN10_simgene_T.txt --out_folder={output_folder}"""

RETRIEVAL_COMMAND_TEMPLATE="""python scrna.py retrieval {reduced_data_folder} \
--out_folder={output_folder}"""

SLURM_TRANSFORM_COMMAND="""sbatch --array=0-{num_jobs} --mail-user {email} \
--output {out_folder}/scrna_transform_array_%A_%a.out
--error {err_folder}/scrna_transform_array_%A_%a.err slurm_transform_array.sh"""

SLURM_RETRIEVAL_COMMAND="""sbatch --array=0-{num_jobs} --mail-user {email} \
--output {out_folder}/scrna_retrieval_array_%A_%a.out
--error {err_folder}/scrna_retrieval_array_%A_%a.err -d afterok:{depends} slurm_retrieval_array.sh"""

class SafeDict(dict):
    """Allows for string formatting with unused keyword arguments
    """
    def __missing__(self, key):
        return '{' + key + '}'


def write_out_command_dict(cmd_dict, path):
    with open(path, 'w') as f:
        for value in cmd_dict.values():
            f.write(value + '\n')

class Experiment(object):
    def __init__(self, working_dir_path=None):
        if not working_dir_path:
            # Automatically create a unique working directory for the experiment
            time_str = time.strftime("%Y_%m_%d-%H:%M:%S")
            working_dir_path = join(DEFAULT_WORKING_DIR_ROOT, time_str)
        makedirs(working_dir_path)
        self.working_dir_path = working_dir_path


        
    def prepare(self, models_file=DEFAULT_MODELS_FILE):
        """
        Args:
            models_file: path to a file which contains, on each line, the path
                         to the folder containing a trained neural network
                         model.
        """
        # Prep Transform commands
        print("Preparing Transform commands...")
        with open(models_file) as f:
            model_folders = f.readlines()
        model_folders = [s.strip() for s in model_folders]
        transform_commands = {}
        transform_data_folders = {}
        for model_folder in model_folders:
            model_name = basename(normpath(model_folder))
            # path to output location, where the transformed data will be written to
            reduced_data_folder = join(self.working_dir_path, "data_transformed_by_" + model_name)
            transform_data_folders[model_name] = reduced_data_folder
            transform_commands[model_name] = string.Formatter().vformat(REDUCE_COMMAND_TEMPLATE, (), SafeDict(trained_nn_folder=model_folder, output_folder=reduced_data_folder))
        # write each of the command lines for transformation to a file, to be consumed by the slurm jobs
        write_out_command_dict(transform_commands, 'transform_commands.list')
        self.transform_commands = transform_commands
        self.transform_data_folders = transform_data_folders
        # Prep Retrieval commands
        print("Preparing Retrieval commands...")
        retrieval_dir = join(self.working_dir_path, "retrieval_results")
        makedirs(retrieval_dir)
        retrieval_commands = {}
        retrieval_result_folders = {}
        for model_name, transformed_data_folder in transform_data_folders.items():
            # path to output location, where the retrieval test results will be written to
            retrieval_result_folder = join(retrieval_dir, model_name)
            retrieval_commands[model_name] = string.Formatter().vformat(RETRIEVAL_COMMAND_TEMPLATE, (), SafeDict(reduced_data_folder=transformed_data_folder, output_folder=retrieval_result_folder))
            retrieval_result_folders[model_name] = retrieval_result_folder
        # write each of the command lines for retrieval testing to a file, to be consumed by the slurm jobs
        write_out_command_dict(retrieval_commands, 'retrieval_commands.list')
        self.retrieval_commands = retrieval_commands
        self.retrieval_dir = retrieval_dir
        self.retrieval_result_folders = retrieval_result_folders
        print("Preparation complete, commands constructed.")

    def run(self, email_addr):
        # First transform the data
        slurm_transform_out_folder = join(self.working_dir_path, "slurm_transform_out")
        makedirs(slurm_transform_out_folder)
        num_jobs = len(self.transform_commands)
        transform_cmd = SLURM_TRANSFORM_COMMAND.format(num_jobs=str(num_jobs-1), email=email_addr, out_folder=slurm_transform_out_folder, err_folder=slurm_transform_out_folder)
        print("Running slurm array job to reduce dimensions using models...")
        result = subprocess.run(transform_cmd.split(), stdout=subprocess.PIPE)
        transform_job_id = int(result.stdout.decode("utf-8").strip().split()[-1])
        print("Slurm array job submitted, id: ", transform_job_id)
        # Then run retrieval (after transformation completes)
        slurm_retrieval_out_folder = join(self.working_dir_path, "slurm_retrieval_out")
        makedirs(slurm_retrieval_out_folder)
        num_jobs = len(self.retrieval_commands)
        retrieval_cmd = SLURM_RETRIEVAL_COMMAND.format(num_jobs=str(num_jobs-1), email=email_addr, out_folder=slurm_retrieval_out_folder, err_folder=slurm_retrieval_out_folder, depends=transform_job_id)
        print("Running slurm array job to conduct retrieval test using each model...")
        result = subprocess.run(retrieval_cmd.split(), stdout=subprocess.PIPE)
        retrieval_job_id = int(result.stdout.decode("utf-8").strip().split()[-1])
        print("Slurm array job submitted, id: ", retrieval_job_id)
        # Must wait for retrieval jobs to finish in order to use their results
        print("Waiting for retrieval jobs to finish...")
        wait_cmd = "srun -J waiter -d afterok:{depends} -p zbj1 echo '(done waiting)'"
        subprocess.run(wait_cmd.format(depends=retrieval_job_id).split())

    def get_avg_score_for_each_cell_type(self, path_to_csv):
        with open(path_to_csv) as csv_file:
            reader = csv.DictReader(csv_file)
            # Dict of <cell_type: scores[]>
            cell_type_scores_dict = defaultdict(list)
            for row in reader:
                cur_cell_type = row['celltype']
                cur_score = float(row['mean average precision'])
                cell_type_scores_dict[cur_cell_type].append(cur_score)
        # Calculate average retrieval performance for each cell type accross the datasets
        cell_type_avg_score_dict = dict()
        for cell_type, scores_list in cell_type_scores_dict.items():
            cell_type_avg_score_dict[cell_type] = np.mean(scores_list)
        return cell_type_avg_score_dict

    def create_overall_results_table(self):
        '''Compiles results into various tables:
        - overall table - contains all of the raw data in a single table

        root_folder: the path to the folder that contains a folder for each model
        Returns: overall results table
        '''
        overall_results_fieldnames = ['model', 'cell_type', 'avg_score']
        overall_results = []
        for model_name, results_folder in self.retrieval_result_folders.items():
            # Iterate through models
            print(model_name)
            results_file = join(results_folder, 'retrieval_summary.csv')
            cell_types_and_scores = self.get_avg_score_for_each_cell_type(results_file)
            for cell_type, score in cell_types_and_scores.items():
                # Iterate through cell types
                overall_results.append({'model': model_name, 'cell_type': cell_type, 'avg_score': score})
        with open(join(self.working_dir_path, 'full_results_table.csv'), 'w') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=overall_results_fieldnames)
            writer.writeheader()
            for row in overall_results:
                writer.writerow(row)
        return overall_results

    def compile_results(self):
        """Create a summary of the retrieval experiment results
        """
        print("Compiling results...")
        results = self.create_overall_results_table()


if __name__ == '__main__':
    args = docopt(__doc__, version='experiment 0.1')
    exp = Experiment()
    exp.prepare()
    exp.run(args['<email_address>'])
    exp.compile_results()
