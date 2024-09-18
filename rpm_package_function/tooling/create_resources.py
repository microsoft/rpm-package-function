#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Creates resources for the rpm package function in Azure."""

import argparse
import logging
import sys
from pathlib import Path

from rpm_package_function.tooling import common_logging
from rpm_package_function.tooling.bicep_deployment import BicepDeployment
from rpm_package_function.tooling.func_app import FuncAppBundle
from rpm_package_function.tooling.poetry import extract_requirements
from rpm_package_function.tooling.resource_group import create_rg

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


def main() -> None:
    """Create resources."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "resource_group", help="The name of the resource group to create resources in."
    )
    parser.add_argument(
        "--location",
        default="eastus",
        help="The location of the resources to create. A list of location names can be obtained by running 'az account list-locations --query \"[].name\"'",
    )
    parser.add_argument(
        "--suffix",
        help="Unique suffix for the repository name. If not provided, a random suffix will be generated. Must be 14 characters or fewer.",
    )
    parser.add_argument(
        "--upload-directory",
        default="upload",
        help="Path within the storage container to upload packages to.",
    )
    args = parser.parse_args()

    if args.suffix and len(args.suffix) > 14:
        raise ValueError("Suffix must be 14 characters or fewer.")

    # Create the resource group
    create_rg(args.resource_group, args.location)

    # Ensure requirements.txt exists
    extract_requirements(Path("requirements.txt"))

    # Create resources with Bicep
    #
    # Set up parameters for the Bicep deployment
    common_parameters = {}
    if args.suffix:
        common_parameters["suffix"] = args.suffix

    initial_parameters = {
        "use_shared_keys": False,
        "upload_directory": args.upload_directory,
    }
    initial_parameters.update(common_parameters)

    # Use the same deployment name as the resource group
    deployment_name = args.resource_group

    initial_resources = BicepDeployment(
        deployment_name=deployment_name,
        resource_group_name=args.resource_group,
        template_file=Path("rg.bicep"),
        parameters=initial_parameters,
        description="initial resources",
    )
    initial_resources.create()

    outputs = initial_resources.outputs()
    log.debug("Deployment outputs: %s", outputs)
    base_url = outputs["base_url"]
    function_app_name = outputs["function_app_name"]
    package_container = outputs["package_container"]
    python_container = outputs["python_container"]
    storage_account = outputs["storage_account"]

    # Create the function app
    funcapp = FuncAppBundle(
        name=function_app_name,
        resource_group=args.resource_group,
        storage_account=storage_account,
        python_container=python_container,
        parameters=common_parameters,
    )

    with funcapp as cm:
        cm.deploy()
        cm.wait_for_event_trigger()

    # At this point the function app exists and the event trigger exists, so the
    # event grid deployment can go ahead.
    event_grid_deployment = BicepDeployment(
        deployment_name=f"{deployment_name}_eg",
        resource_group_name=args.resource_group,
        template_file=Path("rg_add_eventgrid.bicep"),
        parameters=common_parameters,
        description="Event Grid trigger configuration",
    )
    event_grid_deployment.create()

    # Inform the user of success!
    auth_variable = f"AZURE_STORAGE_TOKEN_{storage_account.upper()}"

    print(
        f"""The repository has been created!
Upload packages to the '{args.upload_directory}/' directory in the
'{package_container}' container in the '{storage_account}' storage account.
The function app '{function_app_name}' will be triggered by new packages
in that container and regenerate the repository.

To download packages:
  - Copy the 'dnf-plugins/dnf-plugin-azure-auth.py' module to the Python
    system site-packages directory under `dnf-plugins`.
    - The system site packages directory can be determined by running:

      python3 -c 'import site; print(site.getsitepackages())'

  - Create a repository file '/etc/yum.repos.d/{storage_account}.repo' with the following content
    for each distribution you want to support:

[{storage_account}`dist`]
name={storage_account}`dist`
baseurl={base_url}/`dist-letters`/`dist-numbers`/
enabled=1
gpgcheck=0
skip_if_unavailable=1

    For example, to support a distribution 'el8' the repository file would look like:

[{storage_account}el8]
name={storage_account}el8
baseurl={base_url}/el/8/
enabled=1
gpgcheck=0
skip_if_unavailable=1

    and a distribution 'fc34' would look like:

[{storage_account}fc34]
name={storage_account}fc34
baseurl={base_url}/fc/34/
enabled=1
gpgcheck=0
skip_if_unavailable=1

    Multiple repository definitions can exist in the same file.

  - Create an authentication token in the environment with the name '{auth_variable}'.

    export {auth_variable}=$(az account get-access-token --query accessToken --output tsv --resource https://storage.azure.com)

    You must have 'Storage Blob Data Reader' access to the storage account.
  - Now, use yum/dnf as normal!"""
    )


def run() -> None:
    """Entrypoint which sets up logging."""
    common_logging(__name__, __file__, stream=sys.stderr)
    main()


if __name__ == "__main__":
    run()
