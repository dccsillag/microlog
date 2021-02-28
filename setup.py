from setuptools import setup
import os


this_directory = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(this_directory, 'README.md')) as file:
    long_description = file.read()



setup(
    name="logrun",
    version="0.1.1",
    description="A convenient experiment logging package for Python",
    long_description=long_description,
    long_description_content_type='text/markdown',
    url="https://github.com/dccsillag/logrun",
    author="Daniel Csillag",
    author_email="dccsillag@gmail.com",
    license="MIT",
    packages=['logrun', 'logrun.utils'],
    install_requires=['dill',
                      'psutil',
                      'xxhash',
                      'gitpython'],

    # TODO: classifiers
)
