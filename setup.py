from setuptools import setup, find_namespace_packages

setup(
    name="viral_ngs.core",
    version="0.1",
    packages=find_namespace_packages(include=["viral_ngs.*"]),
    entry_points={
        "console_scripts": [
            "broad_utils = viral_ngs.core.broad_utils.__main__",
            "file_utils = viral_ngs.core.file_utils.__main__",
            "read_utils = viral_ngs.core.read_utils.__main__",
            "illumina = viral_ngs.core.illumina.__main__",
            "reports = viral_ngs.core.reports.__main__"
        ]
    }
)