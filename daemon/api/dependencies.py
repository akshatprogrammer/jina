from pathlib import Path
from typing import Dict, List, Optional
from pydantic import FilePath
from fastapi import HTTPException, UploadFile, File
from pydantic.errors import PathNotAFileError
from pydantic.main import BaseModel

from jina import Flow
from jina.enums import PeaRoleType, SocketType
from jina.helper import ArgNamespace, cached_property

from ..helper import get_workspace_path
from ..models.enums import WorkspaceState
from ..stores import workspace_store as store
from ..models.workspaces import WorkspaceItem
from ..models import DaemonID, FlowModel, PodModel, PeaModel


class FlowDepends:
    def __init__(self, workspace_id: DaemonID, filename: str) -> None:
        self.workspace_id = workspace_id
        self.filename = filename
        self.localpath = self.validate()
        self.id = DaemonID('jflow')
        self.params = FlowModel(
            uses=self.filename, workspace_id=self.workspace_id.jid, identity=self.id.jid
        )

    @property
    def command(self) -> str:
        return (
            f'jina flow --uses /workspace/{self.params.uses} '
            f'--identity {self.params.identity} '
            f'--workspace-id {self.params.workspace_id}'
        )

    @property
    def ports(self) -> Dict[str, str]:
        # TODO: Super ugly way of knowing, if the yaml file has port_expose set
        f = Flow.load_config(str(self.localpath))
        port_expose = f._common_kwargs.get('port_expose')
        # TODO: How to set port_expose which starting a Flow via CLI
        return {f'{port_expose}/tcp': port_expose} if port_expose else {}

    def validate(self):
        try:
            return FilePath.validate(
                Path(get_workspace_path(self.workspace_id, self.filename))
            )
        except PathNotAFileError as e:
            raise HTTPException(
                status_code=404,
                detail=f'File `{self.filename}` not found in workspace `{self.workspace_id}`',
            )


class PeaDepends:
    _kind = 'pea'

    def __init__(self, workspace_id: DaemonID, pea: PeaModel):
        # Deepankar: adding quotes around PeaModel breaks things
        self.workspace_id = workspace_id
        self.params = pea
        self.id = DaemonID('jpea')
        self.validate()

    @property
    def command(self) -> str:
        return f'jina {self._kind} {" ".join(ArgNamespace.kwargs2list(self.params.dict(exclude={"log_config"})))}'

    @cached_property
    def ports(self) -> Dict:
        _mapping = {
            'port_in': 'socket_in',
            'port_out': 'socket_out',
            'port_ctrl': 'socket_ctrl',
        }
        # Map only "bind" ports for HEAD, TAIL & SINGLETON
        if PeaRoleType.from_string(self.params.pea_role) != PeaRoleType.PARALLEL:
            return {
                f'{getattr(self.params, i)}/tcp': getattr(self.params, i)
                for i in self.params.__fields__
                if i in _mapping
                and SocketType.from_string(
                    getattr(self.params, _mapping[i], 'PAIR_BIND')
                ).is_bind
            }
        else:
            return {f'{self.params.port_ctrl}/tcp': self.params.port_ctrl}

    def validate(self):
        # Each pea is inside a container
        # TODO: Handle host if pea uses a docker image
        self.params.host_in = 'host.docker.internal'
        self.params.host_out = 'host.docker.internal'
        self.params.identity = self.id
        self.params.workspace_id = self.workspace_id


class PodDepends(PeaDepends):
    def __init__(self, workspace_id: DaemonID, pod: PodModel):
        self.workspace_id = workspace_id
        self.params = pod
        self.validate()
        self.id = DaemonID('jpod')


class WorkspaceDepends:
    def __init__(
        self, id: Optional[DaemonID] = None, files: List[UploadFile] = File(None)
    ) -> None:
        self.id = id if id else DaemonID('jworkspace')
        self.files = files

        from ..tasks import __task_queue__

        if self.id not in store:
            # when id doesn't exist in store, create it.
            store.add(id=self.id, value=WorkspaceState.PENDING)
            __task_queue__.put((self.id, self.files))

        if self.id in store and store[self.id].state == WorkspaceState.ACTIVE:
            # when id exists in the store and is "active", update it.
            store.update(id=self.id, value=WorkspaceState.PENDING)
            __task_queue__.put((self.id, self.files))

        self.j = {self.id: store[self.id]}
