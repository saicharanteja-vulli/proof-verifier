import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Source(Strict):
    path: str = Field(min_length=1, max_length=180)
    content: str = Field(max_length=150_000)

    @field_validator('path')
    @classmethod
    def safe_path(cls, value):
        path = PurePosixPath(value)
        if not re.fullmatch(r'[a-zA-Z0-9_./-]+', value) or path.is_absolute() or any(p in {'..', '.', ''} for p in value.split('/')):
            raise ValueError('Use a relative project file path without traversal.')
        if any(p.startswith('.') or p in {'data','node_modules','__pycache__','venv'} for p in path.parts):
            raise ValueError('Hidden files, private data and runtimes are excluded.')
        if path.suffix not in {'.py','.html','.css','.js','.json','.txt','.md','.toml','.svg'}:
            raise ValueError('Unsupported source file type.')
        return value

class Check(Strict):
    name: str = Field(min_length=1,max_length=100)
    method: Literal['GET','POST','PUT','PATCH','DELETE'] = 'GET'
    path: str = Field(min_length=1,max_length=500)
    headers: dict[str,str] = Field(default_factory=dict,max_length=12)
    body: dict | list | None = None
    expected_status: int = Field(ge=100,le=599)
    expected_json: dict | None = None

    @field_validator('path')
    @classmethod
    def local_path(cls,value):
        if not value.startswith('/') or value.startswith('//') or any(ord(c)<32 for c in value) or '#' in value:
            raise ValueError('Use a local API path, such as /api/status.')
        return value

    @model_validator(mode='after')
    def bounded(self):
        if len(json.dumps(self.model_dump()).encode()) > 12_000:
            raise ValueError('Check payload is too large.')
        for k,v in self.headers.items():
            if not re.fullmatch(r'[A-Za-z0-9-]{1,60}',k) or any(ord(c)<32 or ord(c)>126 for c in v) or len(v)>1000:
                raise ValueError('Invalid HTTP header.')
            if k.lower() in {'host','content-length','transfer-encoding','connection'}:
                raise ValueError('Transport headers are controlled by Proof.')
        return self

class SourceInput(Strict):
    github_import_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    title: str = Field(min_length=1,max_length=100)
    requirement: str = Field(min_length=10,max_length=2000)
    entrypoint: str = Field(default='app:app',pattern=r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$')
    baseline: list[Source] = Field(min_length=1,max_length=80)
    candidate: list[Source] = Field(min_length=1,max_length=80)

    @model_validator(mode='after')
    def consistent(self):
        for files in (self.baseline,self.candidate):
            paths=[f.path for f in files]
            if len(set(paths))!=len(paths): raise ValueError('Duplicate source paths.')
            if sum(len(f.content.encode()) for f in files)>2_000_000: raise ValueError('Each version is limited to 2 MB.')
            module=self.entrypoint.split(':')[0].replace('.','/')
            if module+'.py' not in paths and module+'/__init__.py' not in paths:
                raise ValueError('Entrypoint module is missing from a source version.')
        return self

class Review(SourceInput):
    checks: list[Check] = Field(min_length=1,max_length=12)
    execution_consent: Literal[True]
    proposal_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    tests_reviewed: bool = False

    @model_validator(mode='after')
    def checks_valid(self):
        if len({c.name for c in self.checks})!=len(self.checks): raise ValueError('Check names must be unique.')
        if (self.proposal_id or self.github_import_id) and not self.tests_reviewed: raise ValueError('Review the proposed tests before running.')
        return self

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
