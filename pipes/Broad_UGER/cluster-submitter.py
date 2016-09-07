#!/usr/bin/env python3
import os
import sys
import re
from snakemake.utils import read_job_properties

LOGDIR = sys.argv[-2]
jobscript = sys.argv[-1]
mo = re.match(r'(\S+)/snakejob\.\S+\.(\d+)\.sh', jobscript)
assert mo
sm_tmpdir, sm_jobid = mo.groups()
props = read_job_properties(jobscript)

# set up job name, project name
jobname = "{rule}-{jobid}".format(rule=props["rule"], jobid=sm_jobid)
if props["params"].get("logid"):
    jobname = "{rule}-{id}".format(rule=props["rule"], id=props["params"]["logid"])
cmdline = "qsub -P {proj_name} -N {jobname} -cwd -r y ".format(proj_name='sabeti_lab', jobname=jobname)

# log file output
cmdline += "-o {logdir} -e {logdir} ".format(logdir=LOGDIR)

# pass memory resource request to cluster
mem = props.get('resources', {}).get('mem')
cores = props.get('resources', {}).get('cores')
cores = cores or 1
if mem:
    # on UGER, memory requests are per-core (according to BITS as of Sept. 6, 2016)
    mem_per_core = round((int(mem)*1024)/int(cores), 2)
    cmdline += ' -l h_vmem={}M,h_rss={}M '.format(  mem_per_core, round(1.2 * mem_per_core, 2)  )
    if mem >= 15 or (cores and cores >= 4):
        cmdline += ' -R y '

if cores:
    cmdline += ' -pe smp {} -binding linear:{} '.format(int(cores), int(cores))

# rule-specific UGER parameters (e.g. queue)
cmdline += props["params"].get("UGER", "") + " "

# figure out job dependencies
dependencies = set(sys.argv[1:-2])
if dependencies:
    cmdline += "-hold_jid '{}' ".format(",".join(dependencies))

# the actual job
cmdline += jobscript

# the success file
cmdline += " %s/%s.jobfinished" % (sm_tmpdir, sm_jobid)

# the part that strips bsub's output to just the job id
cmdline += r" | tail -1 | cut -f 3 -d \ "

# call the command
os.system(cmdline)
