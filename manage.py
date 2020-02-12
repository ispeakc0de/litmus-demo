#!/usr/bin/env python3

import argparse
import os
import json
import sys
import time
from datetime import datetime
import subprocess
import yaml

# Subcommand options

def start(args):
    """
    Start a GKE Cluster with Zebrium's demo environment deployed.
    """
    print(f"Starting GKE cluster in project {args.project} with name {args.name} in zone {args.zone}")

    # Ensure GCloud SDK is up to date
    os.system("gcloud components update")

    # Set GCloud project
    os.system(f"gcloud config set project \"{args.project}\"")

    # Spinup cluster
    os.system(f"gcloud container clusters create {args.name} --zone {args.zone}")

    # Get kubectl credentials
    os.system(f"gcloud container clusters get-credentials {args.name} --zone {args.zone}")

    print("\nGKE Cluster Running with following nodes:\n")
    os.system(f"kubectl get nodes")

    # Deploy Zebrium Collector using Helm
    ze_deployment_name = "zebrium-k8s-demo"
    ze_api_url = "https://zapi03.zebrium.com"
    os.system("sleep 60") # Wait 1 min for cluster to finish setting up fully
    os.system("kubectl create namespace zebrium")
    os.system(f"helm install zlog-collector --namespace zebrium --set zebrium.deployment={ze_deployment_name},zebrium.collectorUrl={ze_api_url},zebrium.authToken={args.key} --repo https://raw.githubusercontent.com/zebrium/ze-kubernetes-collector/master/charts zlog-collector")

    # Deploy all demo apps
    os.system("kubectl create -f ./deploy/sock-shop.yaml")
    os.system("kubectl create -f ./deploy/random-log-counter.yaml")

    # Deploy kafka demo app
    os.system("kubectl create namespace kafka")
    os.system("helm repo add confluentinc https://confluentinc.github.io/cp-helm-charts/")
    os.system("helm repo update")
    os.system("helm install kafka-cluster confluentinc/cp-helm-charts --namespace=kafka")
    os.system('kubectl annotate sts/kafka-cluster-cp-kafka litmuschaos.io/chaos="true" -n kafka')

    # Deploy Litmus ChaosOperator to run Experiments that create incidents
    os.system("kubectl apply -f https://litmuschaos.github.io/pages/litmus-operator-v1.0.0.yaml")

    # Install Litmus Experiments
    os.system("kubectl create -f https://hub.litmuschaos.io/api/chaos?file=charts/generic/experiments.yaml -n sock-shop")
    os.system("kubectl create -f https://hub.litmuschaos.io/api/chaos?file=charts/kafka/experiments.yaml -n kafka")

    # Create the chaos serviceaccount with permissions needed to run the generic K8s experiments
    os.system("kubectl create -f ./deploy/litmus-rbac.yaml")

    # Get ingress IP address
    print("\nIngress Details:\n")
    os.system("kubectl get ingress basic-ingress --namespace=sock-shop")

    try:
        ingress_ip = \
        json.loads(os.popen('kubectl get ingress basic-ingress --namespace=sock-shop -o json').read())["status"][
            "loadBalancer"]["ingress"][0]["ip"]
        print(f"\nYou can access the web application in a few minutes at: http://{ingress_ip}")
    except:
        print("Ingress still being setup. Use the following command to get the IP later:")
        print("\tkubectl get ingress basic-ingress --namespace=sock-shop")

    print("\nFinished creating cluster. Please wait at least 15 minutes for environment to become fully initalised.")
    print("The ingress to access the web application from your browser can take at least 5 minutes to create.")

def stop(args):
    """
    Shutdown the GKE Cluster with Zebrium's demo environment deployed.
    """
    print(f"Stopping GKE cluster in project {args.project} with name {args.name} in zone {args.zone}")

    # Set GCloud project
    os.system(f"gcloud config set project \"{args.project}\"")

    # Stop cluster
    os.system(f"gcloud container clusters delete {args.name} --zone {args.zone}")

class ExperimentResult(object):
    """
    Holds Experiment Result
    """

    def __init__(self, name:str, status:str, startTime:datetime):
        self.name = name
        self.status = status
        self.startTime = startTime

def run_experiment(experiment: str, delay: int = 0):
    """
    Run a specific experiment

    :param experiment:  The name of the experiment as defined in the YAML, i.e. container-kill
    :param ramp_time:   The number of seconds to delay experiment after setup to avoid confusing setup events with experiment events in Zebrium
    :return:            ExperimentResult object with results of experiment
    """
    print("***************************************************************************************************")
    print(f"* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Experiment: {experiment}")
    print("***************************************************************************************************")

    experiment_file = experiment + ".yaml"

    # Set namespace to check
    with open(f"./litmus/{experiment_file}") as f:
        spec = yaml.load(f, Loader=yaml.FullLoader)
        result_name = spec['metadata']['name']
        namespace = spec['metadata']['namespace']

        # Create temp file with updated RAMP_TIME
        spec['spec']['experiments'][0]['spec']['components'].append({'name': 'RAMP_TIME', 'value': str(delay)})
        with open(r"temp.yaml", 'w') as temp:
            yaml.dump(spec, temp)

    print(f"Running Litmus ChaosEngine Experiment {experiment_file} in namespace {namespace} with delay {delay} seconds...")
    print(f"Deploying {experiment_file}...")
    os.system(f"kubectl delete chaosengine {result_name} -n {namespace}")
    os.system(f"kubectl create -f temp.yaml -n {namespace}")

    # Check status of experiment execution
    startTime = datetime.now()
    print(f"{startTime.strftime('%Y-%m-%d %H:%M:%S')} Running experiment...")
    expStatusCmd = "kubectl get chaosengine " + result_name + " -o jsonpath='{.status.experiments[0].status}' -n " + namespace
    while subprocess.check_output(expStatusCmd, shell=True).decode('unicode-escape') != "Execution Successful":
        print(".")
        os.system("sleep 10")

    # View experiment results
    print(f"\nkubectl describe chaosresult {result_name}-{experiment} -n {namespace}")
    os.system(f"kubectl describe chaosresult {result_name}-{experiment} -n {namespace}")

    # Delete temp file
    os.system('rm temp.yaml')

    # Store Experiment Result
    status = subprocess.check_output("kubectl get chaosresult " + result_name + "-" + experiment + " -n " + namespace + " -o jsonpath='{.spec.experimentstatus.verdict}'", shell=True).decode('unicode-escape')
    return ExperimentResult(experiment, status, startTime)

def test(args):
    """
    Run Litmus ChaosEngine Experiments inside Zebrium's demo environment.
    Each experiment is defined under its own yaml file under the /litmus directory. You can run
    a specific experiment by specifying a test name that matches one of the yaml file names in the directory
    but by default all '*' experiments will be run with 20 minute wait period between each experiment
    to ensure Zebrium doesn't cluster the incidents together into one incident
    """
    experiments = os.listdir('./litmus')
    experiment_results = []

    if args.test == '*':
        # Run all experiments in /litmus directory with wait time between them
        print(f"Running all Litmus ChaosEngine Experiments with {args.wait} mins wait time between each one...")
        for experiment_file in experiments:
            result = run_experiment(experiment_file.replace('.yaml', ''), args.delay)
            experiment_results.append(result)
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Waiting {args.wait} mins before running next experiment...")
            time.sleep(args.wait * 60)
    else:
        # Check experiment exists
        experiment_file = args.test + ".yaml"
        if experiment_file in experiments:
            result = run_experiment(args.test, args.delay)
            experiment_results.append(result)
        else:
            print(f"ERROR: {experiment_file} not found in ./litmus directory. Please check the name and try again.")
            sys.exit(2)

    # Print out experiment result summary
    print("***************************************************************************************************")
    print(f"* Experiments Result Summary")
    print("***************************************************************************************************\n")
    headers = ["#", "Start Time", "Experiment", "Status"]
    row_format = "{:>25}" * (len(headers) + 1)
    print(row_format.format("", *headers))
    i = 1
    for result in experiment_results:
        print(row_format.format("", str(i), result.startTime.strftime('%Y-%m-%d %H:%M:%S'), result.name, result.status))
        i += 1
    print("\n")

if __name__ == "__main__":

    # Add command line arguments
    parser = argparse.ArgumentParser(description='Spin up Zebrium Demo Environment on Kubernetes.')
    subparsers = parser.add_subparsers()

    # Start command
    parser_start = subparsers.add_parser("start", help="Start a GKE Cluster with Zebrium's demo environment deployed.")
    parser_start.add_argument("-p", "--project", type=str,
                        help="Set GCloud Project to spin GKE cluster up in")
    parser_start.add_argument("-z", "--zone", type=str, default="us-central1-a",
                        help="Set GCloud Zone to spin GKE cluster up in")
    parser_start.add_argument("-n", "--name", type=str, default="zebrium-k8s-demo",
                        help="Set GKE cluster name")
    parser_start.add_argument("-k", "--key", type=str,
                        help="Set Zebrium collector key for demo account")
    parser_start.set_defaults(func=start)

    # Test command
    parser_test = subparsers.add_parser("test", help="Run Litmus ChaosEngine Experiments inside Zebrium's demo environment.")
    parser_test.add_argument("-t", "--test", type=str, default="*",
                             help="Name of test to run based on yaml file name under /litmus folder. '*' runs all of them with wait time between each experiement.")
    parser_test.add_argument("-w", "--wait", type=int, default=15,
                             help="Number of minutes to wait between experiments. Defaults to 20 mins to avoid Zebrium clustering incidents together.")
    parser_test.add_argument("-d", "--delay", type=int, default=660,
                             help="Delay time in seconds between setting up experiment and running it. Defaults to 660 seconds.")
    parser_test.set_defaults(func=test)

    # Stop command
    parser_stop = subparsers.add_parser("stop", help="Shutdown the GKE Cluster with Zebrium's demo environment deployed.")
    parser_stop.add_argument("-p", "--project", type=str,
                        help="Set GCloud Project to spin GKE cluster up in")
    parser_stop.add_argument("-z", "--zone", type=str, default="us-central1-a",
                        help="Set GCloud Zone to spin GKE cluster up in")
    parser_stop.add_argument("-n", "--name", type=str, default="zebrium-k8s-demo",
                        help="Set GKE cluster name")
    parser_stop.set_defaults(func=stop)

    args = parser.parse_args()
    args.func(args)
