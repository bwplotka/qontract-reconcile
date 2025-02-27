import logging
import datetime

from threading import Lock

import reconcile.openshift_base as osb
import reconcile.queries as queries
import reconcile.jenkins_plugins as jenkins_base
import reconcile.utils.threaded as threaded

from reconcile.utils.openshift_resource import OpenshiftResource as OR
from reconcile.jenkins_job_builder import get_openshift_saas_deploy_job_name
from reconcile.utils.oc import OC_Map
from reconcile.utils.gitlab_api import GitLabApi
from reconcile.utils.saasherder import SaasHerder, Providers, \
    UNIQUE_SAAS_FILE_ENV_COMBO_LEN
from reconcile.utils.sharding import is_in_shard
from reconcile.utils.defer import defer

_trigger_lock = Lock()


@defer
def run(dry_run,
        trigger_type,
        integration,
        integration_version,
        thread_pool_size,
        internal,
        use_jump_host,
        v1=True,
        v2=True,
        defer=None):
    """Run trigger integration

    Args:
        dry_run (bool): Is this a dry run
        trigger_type (str): Indicates which method to call to get diff
                            and update state
        integration (string): Name of calling integration
        integration_version (string): Version of calling integration
        thread_pool_size (int): Thread pool size to use
        internal (bool): Should run for internal/extrenal/all clusters
        use_jump_host (bool): Should use jump host to reach clusters
        v1 (bool): Should trigger for v1 SaaS files
        v2 (bool): Should trigger for v2 SaaS files

    Returns:
        bool: True if there was an error, False otherwise
    """
    saasherder, jenkins_map, oc_map, settings, error = \
        setup(
            thread_pool_size=thread_pool_size,
            internal=internal,
            use_jump_host=use_jump_host,
            integration=integration,
            integration_version=integration_version,
            v1=v1,
            v2=v2
        )
    if error:
        return error
    defer(lambda: oc_map.cleanup())

    trigger_specs, diff_err = saasherder.get_diff(trigger_type, dry_run)
    # This will be populated by 'trigger' in the below loop and
    # we need it to be consistent across all iterations
    already_triggered = set()

    errors = \
        threaded.run(
            trigger,
            trigger_specs,
            thread_pool_size,
            dry_run=dry_run,
            saasherder=saasherder,
            jenkins_map=jenkins_map,
            oc_map=oc_map,
            already_triggered=already_triggered,
            settings=settings,
            trigger_type=trigger_type,
            integration=integration,
            integration_version=integration_version
        )
    errors.append(diff_err)

    return any(errors)


def setup(thread_pool_size,
          internal,
          use_jump_host,
          integration,
          integration_version,
          v1,
          v2):
    """Setup required resources for triggering integrations

    Args:
        thread_pool_size (int): Thread pool size to use
        internal (bool): Should run for internal/extrenal/all clusters
        use_jump_host (bool): Should use jump host to reach clusters
        integration (string): Name of calling integration
        integration_version (string): Version of calling integration
        v1 (bool): Should trigger for v1 SaaS files
        v2 (bool): Should trigger for v2 SaaS files

    Returns:
        saasherder (SaasHerder): a SaasHerder instance
        jenkins_map (dict): Instance names with JenkinsApi instances
        oc_map (OC_Map): a dictionary of OC clients per cluster
        settings (dict): App-interface settings
        error (bool): True if one happened, False otherwise
    """

    saas_files = queries.get_saas_files(v1=v1, v2=v2)
    if not saas_files:
        logging.error('no saas files found')
        return None, None, None, None, True
    saas_files = [sf for sf in saas_files if is_in_shard(sf['name'])]

    # Remove saas-file targets that are disabled
    for saas_file in saas_files[:]:
        resource_templates = saas_file['resourceTemplates']
        for rt in resource_templates[:]:
            targets = rt['targets']
            for target in targets[:]:
                if target['disable']:
                    targets.remove(target)

    instance = queries.get_gitlab_instance()
    settings = queries.get_app_interface_settings()
    accounts = queries.get_aws_accounts()
    gl = GitLabApi(instance, settings=settings)
    jenkins_map = jenkins_base.get_jenkins_map()
    pipelines_providers = queries.get_pipelines_providers()
    tkn_provider_namespaces = [pp['namespace'] for pp in pipelines_providers
                               if pp['provider'] == 'tekton']

    oc_map = OC_Map(
        namespaces=tkn_provider_namespaces,
        integration=integration,
        settings=settings, internal=internal,
        use_jump_host=use_jump_host,
        thread_pool_size=thread_pool_size)

    saasherder = SaasHerder(
        saas_files,
        thread_pool_size=thread_pool_size,
        gitlab=gl,
        integration=integration,
        integration_version=integration_version,
        settings=settings,
        jenkins_map=jenkins_map,
        accounts=accounts)

    return saasherder, jenkins_map, oc_map, settings, False


def trigger(spec,
            dry_run,
            saasherder,
            jenkins_map,
            oc_map,
            already_triggered,
            settings,
            trigger_type,
            integration,
            integration_version):
    """Trigger a deployment according to the specified pipelines provider

    Args:
        spec (dict): A trigger spec as created by saasherder
        dry_run (bool): Is this a dry run
        saasherder (SaasHerder): a SaasHerder instance
        jenkins_map (dict): Instance names with JenkinsApi instances
        oc_map (OC_Map): a dictionary of OC clients per cluster
        already_triggered (set): A set of already triggered deployments.
                                    It will get populated by this function.
        settings (dict): App-interface settings
        trigger_type (string): Indicates which method to call to update state
        integration (string): Name of calling integration
        integration_version (string): Version of calling integration

    Returns:
        bool: True if there was an error, False otherwise
    """

    # TODO: Convert these into a dataclass.
    saas_file_name = spec['saas_file_name']
    provider_name = spec['pipelines_provider']['provider']

    error = False
    if provider_name == Providers.JENKINS:
        error = _trigger_jenkins(
            spec,
            dry_run,
            saasherder,
            jenkins_map,
            already_triggered,
            settings,
            trigger_type)

    elif provider_name == Providers.TEKTON:
        error = _trigger_tekton(
            spec,
            dry_run,
            saasherder,
            oc_map,
            already_triggered,
            settings,
            trigger_type,
            integration,
            integration_version)

    else:
        error = True
        logging.error(
            f'[{saas_file_name}] unsupported provider: ' +
            f'{provider_name}'
        )

    return error


def _trigger_jenkins(spec,
                     dry_run,
                     saasherder,
                     jenkins_map,
                     already_triggered,
                     settings,
                     trigger_type):
    # TODO: Convert these into a dataclass.
    saas_file_name = spec['saas_file_name']
    env_name = spec['env_name']
    pipelines_provider = spec['pipelines_provider']

    instance_name = pipelines_provider['instance']['name']
    job_name = get_openshift_saas_deploy_job_name(
        saas_file_name, env_name, settings)

    error = False
    to_trigger = _register_trigger(job_name, already_triggered)
    if to_trigger:
        logging.info(['trigger_job', instance_name, job_name])
        if not dry_run:
            jenkins = jenkins_map[instance_name]
            try:
                jenkins.trigger_job(job_name)
            except Exception as e:
                error = True
                logging.error(
                    f"could not trigger job {job_name} " +
                    f"in {instance_name}. details: {str(e)}"
                )

    if not error and not dry_run:
        saasherder.update_state(trigger_type, spec)

    return error


def _trigger_tekton(spec,
                    dry_run,
                    saasherder,
                    oc_map,
                    already_triggered,
                    settings,
                    trigger_type,
                    integration,
                    integration_version):
    # TODO: Convert these into a dataclass.
    saas_file_name = spec['saas_file_name']
    env_name = spec['env_name']
    pipelines_provider = spec['pipelines_provider']

    tkn_namespace_info = pipelines_provider['namespace']
    tkn_namespace_name = tkn_namespace_info['name']
    tkn_cluster_name = tkn_namespace_info['cluster']['name']
    tkn_cluster_console_url = tkn_namespace_info['cluster']['consoleUrl']
    tkn_trigger_resource, tkn_name = _construct_tekton_trigger_resource(
        saas_file_name,
        env_name,
        tkn_cluster_console_url,
        tkn_namespace_name,
        settings,
        integration,
        integration_version
    )

    error = False
    to_trigger = _register_trigger(tkn_name, already_triggered)
    if to_trigger:
        try:
            osb.create(dry_run=dry_run,
                       oc_map=oc_map,
                       cluster=tkn_cluster_name,
                       namespace=tkn_namespace_name,
                       resource_type=tkn_trigger_resource.kind,
                       resource=tkn_trigger_resource)
        except Exception as e:
            error = True
            logging.error(
                f"could not trigger pipeline {tkn_name} " +
                f"in {tkn_cluster_name}/{tkn_namespace_name}. " +
                f"details: {str(e)}"
            )

    if not error and not dry_run:
        saasherder.update_state(trigger_type, spec)

    return error


def _construct_tekton_trigger_resource(saas_file_name,
                                       env_name,
                                       tkn_cluster_console_url,
                                       tkn_namespace_name,
                                       settings,
                                       integration,
                                       integration_version):
    """Construct a resource (PipelineRun) to trigger a deployment via Tekton.

    Args:
        saas_file_name (string): SaaS file name
        env_name (string): Environment name
        tkn_cluster_console_url (string): Cluster console URL of the cluster
                                          where the pipeline runs
        tkn_namespace_name (string): namespace where the pipeline runs
        settings (dict): App-interface settings
        integration (string): Name of calling integration
        integration_version (string): Version of calling integration

    Returns:
        OpenshiftResource: OpenShift resource to be applied
    """
    long_name = f"{saas_file_name}-{env_name}".lower()
    # using a timestamp to make the resource name unique.
    # we may want to revisit traceability, but this is compatible
    # with what we currently have in Jenkins.
    ts = datetime.datetime.utcnow().strftime('%Y%m%d%H%M')  # len 12
    # max name length can be 63. leaving 12 for the timestamp - 51
    name = f"{long_name[:UNIQUE_SAAS_FILE_ENV_COMBO_LEN]}-{ts}"
    body = {
        "apiVersion": "tekton.dev/v1beta1",
        "kind": "PipelineRun",
        "metadata": {
            "name": name
        },
        "spec": {
            "pipelineRef": {
                "name": settings['saasDeployJobTemplate']
            },
            "params": [
                {
                    "name": "saas_file_name",
                    "value": saas_file_name
                },
                {
                    "name": "env_name",
                    "value": env_name
                },
                {
                    "name": "tkn_cluster_console_url",
                    "value": tkn_cluster_console_url
                },
                {
                    "name": "tkn_namespace_name",
                    "value": tkn_namespace_name
                }
            ]
        }
    }
    return OR(body, integration, integration_version,
              error_details=name), long_name


def _register_trigger(name, already_triggered):
    """checks if a trigger should occur and registers as if it did

    Args:
        name (str): unique trigger name to check and
        already_triggered (set): A set of already triggered deployments.
                                 It will get populated by this function.

    Returns:
        bool: to trigger or not to trigger
    """
    global _trigger_lock

    to_trigger = False
    with _trigger_lock:
        if name not in already_triggered:
            to_trigger = True
            already_triggered.add(name)

    return to_trigger
