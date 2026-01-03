from setuptools import setup

setup(
    name="kanban-project-sync",
    version="0.1.0",
    description="Sync markdown kanban boards to GitHub Projects",
    author="Fab2",
    py_modules=["sync_kanban"],
    install_requires=[
        "requests>=2.25.0",
    ],
    entry_points={
        "console_scripts": [
            "kanban-sync=sync_kanban:main",
        ],
    },
    python_requires=">=3.8",
)
