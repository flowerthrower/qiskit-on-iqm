# Copyright 2022 Qiskit on IQM developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Qiskit Backend Provider for IQM backends.
"""
from functools import reduce
from importlib.metadata import version
from typing import Optional, Union
import warnings

from iqm_client import Circuit, Instruction, IQMClient
from iqm_client.util import to_json_dict
import numpy as np
from qiskit import QuantumCircuit
from qiskit.providers import Options

from qiskit_iqm.iqm_backend import IQMBackendBase
from qiskit_iqm.iqm_job import IQMJob
from qiskit_iqm.qiskit_to_iqm import MeasurementKey


class IQMBackend(IQMBackendBase):
    """Backend for executing quantum circuits on IQM quantum computers.

    Args:
        client: client instance for communicating with an IQM server
        **kwargs: optional arguments to be passed to the parent Backend initializer
    """

    def __init__(self, client: IQMClient, **kwargs):
        super().__init__(client.get_quantum_architecture(), **kwargs)
        self.client = client

    @classmethod
    def _default_options(cls) -> Options:
        return Options(shots=1024, calibration_set_id=None)

    @property
    def max_circuits(self) -> Optional[int]:
        return None

    def run(self, run_input: Union[QuantumCircuit, list[QuantumCircuit]], **options) -> IQMJob:
        if self.client is None:
            raise RuntimeError('Session to IQM client has been closed.')

        circuits = [run_input] if isinstance(run_input, QuantumCircuit) else run_input

        if len(circuits) == 0:
            raise ValueError('Empty list of circuits submitted for execution.')

        shots = options.get('shots', self.options.shots)
        calibration_set_id = options.get('calibration_set_id', self.options.calibration_set_id)

        circuits_serialized: list[Circuit] = [self.serialize_circuit(circuit) for circuit in circuits]
        used_qubits: set[str] = reduce(
            lambda qubits, circuit: qubits.union(circuit.all_qubits()),
            circuits_serialized,
            set()
        )
        qubit_mapping = {str(idx): qb for idx, qb in self._idx_to_qb.items() if qb in used_qubits}
        uuid = self.client.submit_circuits(
            circuits_serialized, qubit_mapping=qubit_mapping, calibration_set_id=calibration_set_id, shots=shots
        )
        job = IQMJob(self, str(uuid), shots=shots)
        job.circuit_metadata = [c.metadata for c in circuits]
        return job

    def retrieve_job(self, job_id: str) -> IQMJob:
        """Create and return an IQMJob instance associated with this backend with given job id."""
        return IQMJob(self, job_id)

    def close_client(self):
        """Close IQMClient's session with the authentication server. Discard the client."""
        if self.client is not None:
            self.client.close_auth_session()
        self.client = None

    def serialize_circuit(self, circuit: QuantumCircuit) -> Circuit:
        """Serialize a quantum circuit into the IQM data transfer format.

        Qiskit uses one measurement instruction per qubit (i.e. there is no measurement grouping concept). While
        serializing we do not group any measurements together but rather associate a unique measurement key with each
        measurement instruction, so that the results can later be reconstructed correctly (see :class:`MeasurementKey`
        documentation for more details).

        Args:
            circuit: quantum circuit to serialize

        Returns:
            data transfer object representing the circuit

        Raises:
            ValueError: circuit contains an unsupported instruction or is not transpiled in general
        """
        if len(circuit.qregs) != 1 or len(circuit.qregs[0]) != self.num_qubits:
            raise ValueError(
                f"The circuit '{circuit.name}' does not contain a single quantum register of length {self.num_qubits}, "
                f'which indicates that it has not been transpiled against the current backend.'
            )
        instructions = []
        for instruction, qubits, clbits in circuit.data:
            qubit_names = [str(circuit.find_bit(qubit).index) for qubit in qubits]
            if instruction.name == 'r':
                angle_t = float(instruction.params[0] / (2 * np.pi))
                phase_t = float(instruction.params[1] / (2 * np.pi))
                instructions.append(
                    Instruction(name='phased_rx', qubits=qubit_names, args={'angle_t': angle_t, 'phase_t': phase_t})
                )
            elif instruction.name == 'cz':
                instructions.append(Instruction(name='cz', qubits=qubit_names, args={}))
            elif instruction.name == 'barrier':
                instructions.append(Instruction(name='barrier', qubits=qubit_names, args={}))
            elif instruction.name == 'measure':
                mk = MeasurementKey.from_clbit(clbits[0], circuit)
                instructions.append(Instruction(name='measurement', qubits=qubit_names, args={'key': str(mk)}))
            else:
                raise ValueError(
                    f"Instruction '{instruction.name}' in the circuit '{circuit.name}' is not natively supported. "
                    f'You need to transpile the circuit before execution.'
                )

        try:
            metadata = to_json_dict(circuit.metadata)
        except ValueError:
            warnings.warn(
                f'Metadata of circuit {circuit.name} was dropped because it could not be serialised to JSON.',
            )
            metadata = None

        return Circuit(name=circuit.name, instructions=instructions, metadata=metadata)


class IQMProvider:
    """Provider for IQM backends.

    Args:
        url: URL of the IQM Cortex server

    Keyword Args:
        auth_server_url: URL of the user authentication server, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_SERVER`` environment variable.
        username: Username, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_USERNAME`` environment variable.
        password: Password, if required by the IQM Cortex server.
            Can also be set in the ``IQM_AUTH_PASSWORD`` environment variable.
    """

    def __init__(self, url: str, **user_auth_args):  # contains keyword args auth_server_url, username, password
        self.url = url
        self.user_auth_args = user_auth_args

    def get_backend(self) -> IQMBackend:
        """An IQMBackend instance associated with this provider."""
        client = IQMClient(self.url, client_signature=f'qiskit-iqm {version("qiskit-iqm")}', **self.user_auth_args)
        return IQMBackend(client)
