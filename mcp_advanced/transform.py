import pandas as pd
import json

# Keys to retain when slimming response JSON for generate_csv_viewers.py.
#
# The generator uses:
#   PromptInput          → output.content
#   RubricCriteriaBuilder→ output.criteria
#   TextCollection       → output.persona_selection, output.oracle_events
#   ExternalApp          →
#       metadata.agentRuns[]: agent_run_model, status, trajectoryS3Uri
#       metadata.verifierRuns[]: status, verifierTaskId, verificationResults
#       metadata.deployData (flat env keys — no 'runs' list)
#       metadata.passAt1
AGENT_RUN_KEEP = {'agent_run_model', 'status', 'trajectoryS3Uri', 'id'}
VERIFIER_RUN_KEEP = {'status', 'verifierTaskId', 'verificationResults', 'id'}
VR_RESULT_KEEP = {'id', 'score', 'justification', 'result'}


def _extract_trajectory_map(resp_str):
    """Extract {filename: s3_or_https_uri} from a response/trajectory_urls JSON.

    Scans all ExternalApp steps for agentRuns[].trajectoryS3Uri and
    agentRuns[].taskStepContext.prompt_responses[].agent_trajectory_s3_uri.
    Returns a compact dict suitable for the trajectory_urls column.
    """
    try:
        blob = json.loads(resp_str) if isinstance(resp_str, str) else resp_str
    except (json.JSONDecodeError, TypeError):
        return {}

    out = {}
    turn = (blob.get('turns') or [{}])[0]
    for sv in turn.values():
        if not isinstance(sv, dict) or sv.get('type') != 'ExternalApp':
            continue
        items = sv.get('output', {}).get('items', [])
        if not items or not isinstance(items[0], dict):
            continue
        meta = items[0].get('metadata', {})
        # deployData.runs
        for r in meta.get('deployData', {}).get('runs', []):
            url = r.get('trajectoryS3Uri', '')
            if url:
                fname = url.split('?')[0].split('/')[-1]
                out.setdefault(fname, url)
        # agentRuns
        for ar in meta.get('agentRuns', []):
            url = ar.get('trajectoryS3Uri', '')
            if url:
                fname = url.split('?')[0].split('/')[-1]
                out.setdefault(fname, url)
            for pr in ar.get('taskStepContext', {}).get('prompt_responses', []):
                url2 = pr.get('agent_trajectory_s3_uri', '')
                if url2:
                    fname2 = url2.split('?')[0].split('/')[-1]
                    out.setdefault(fname2, url2)
    return out


def transform(df):
    # Rename 'task' → 'taskid' (what generate_csv_viewers.py expects)
    if 'task' in df.columns and 'taskid' not in df.columns:
        df = df.rename(columns={'task': 'taskid'})

    # Build compact trajectory_urls column {filename: s3_uri} before slimming.
    # Prefer the original trajectory_urls column (may have https:// pre-signed URLs),
    # fall back to the response column (usually has s3:// URIs).
    def build_traj_urls(row):
        traj_map = {}
        if 'trajectory_urls' in row.index and pd.notna(row.get('trajectory_urls')):
            traj_map = _extract_trajectory_map(row['trajectory_urls'])
        if not traj_map and 'response' in row.index and pd.notna(row.get('response')):
            traj_map = _extract_trajectory_map(row['response'])
        return json.dumps(traj_map, separators=(',', ':')) if traj_map else ''

    df['trajectory_urls'] = df.apply(build_traj_urls, axis=1)

    # Drop columns the generator doesn't need
    drop = [c for c in ['attempt_id', 'trajectory_urls_expiration_time'] if c in df.columns]
    if drop:
        df = df.drop(columns=drop)

    def slim(resp_str):
        try:
            r = json.loads(resp_str)
        except (json.JSONDecodeError, TypeError):
            return resp_str

        # Drop top-level keys the generator never touches
        for k in ('before', 'after', 'dataSourceResults', 'metrics'):
            r.pop(k, None)

        turn0 = (r.get('turns') or [{}])[0]

        # Trim each step to only what the generator reads
        for sk in list(turn0.keys()):
            sv = turn0[sk]
            if not isinstance(sv, dict):
                continue
            stype = sv.get('type', '')

            if stype == 'ExternalApp':
                items = sv.get('output', {}).get('items', [])
                if not items or not isinstance(items[0], dict):
                    continue
                # Generator only reads items[0].metadata — drop content & other keys
                meta = items[0].get('metadata', {})
                items[0] = {'metadata': meta}

                # agentRuns: keep only the few keys the generator reads
                slim_ar = []
                for ar in meta.get('agentRuns', []):
                    slim_ar.append({k: ar[k] for k in AGENT_RUN_KEEP if k in ar})
                if slim_ar:
                    meta['agentRuns'] = slim_ar

                # verifierRuns: keep status, verifierTaskId, verificationResults
                # Also slim verificationResults → results[] to only {id,score,justification,result}
                slim_vr = []
                for vr in meta.get('verifierRuns', []):
                    svr = {k: vr[k] for k in VERIFIER_RUN_KEEP if k in vr}
                    # Trim each result inside verificationResults
                    vres = svr.get('verificationResults', {})
                    for vk in list(vres.keys()):
                        vv = vres[vk]
                        if isinstance(vv, dict) and 'results' in vv:
                            vv['results'] = [
                                {rk: res[rk] for rk in VR_RESULT_KEEP if rk in res}
                                for res in vv['results']
                            ]
                    slim_vr.append(svr)
                if slim_vr:
                    meta['verifierRuns'] = slim_vr

                # deployData: drop the large 'runs' list
                meta.get('deployData', {}).pop('runs', None)

                # Drop any other metadata keys the generator doesn't use
                keep_meta = {'agentRuns', 'verifierRuns', 'deployData', 'passAt1'}
                for mk in list(meta.keys()):
                    if mk not in keep_meta:
                        del meta[mk]

            elif stype == 'PromptInput':
                # Keep output.content only
                content = sv.get('output', {}).get('content', '')
                sv['output'] = {'content': content}

            elif stype == 'RubricCriteriaBuilder':
                # Keep output.criteria; trim annotations to only rubric_category
                criteria = sv.get('output', {}).get('criteria', [])
                for c in criteria:
                    ann = c.get('annotations', {})
                    # Generator only uses rubric_category from annotations
                    c['annotations'] = {'rubric_category': ann.get('rubric_category', '')}
                sv['output'] = {'criteria': criteria}

            elif stype == 'TextCollection':
                # Keep only persona_selection and oracle_events
                out = sv.get('output', {})
                slim_out = {}
                for ok in ('persona_selection', 'oracle_events', 'items'):
                    if ok in out:
                        slim_out[ok] = out[ok]
                sv['output'] = slim_out

            # Strip any extra envelope keys from the step itself
            keep_step = {'type', 'output'}
            for stk in list(sv.keys()):
                if stk not in keep_step:
                    del sv[stk]

        return json.dumps(r, separators=(',', ':'))

    df['response'] = df['response'].apply(slim)
    return df

df = pd.read_csv('50ed9d8c-2cdb-4f81-9a99-31d0c9876829.csv')
df = transform(df)
df.to_csv('50ed945-7551-477c-a574-8072c0700a0c_transformed.csv', index=False)